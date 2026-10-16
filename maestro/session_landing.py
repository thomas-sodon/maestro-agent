"""Post-session mechanical landing actuator — move the head SHA without will.

The deepest measured failure mode this module answers is
*read-substitutes-for-write*: a remediation session authors the fix (the
judgment the agent is good at), then narrates "delivered" while the terminal
commit+push is left to the agent's in-session discretion — and the head SHA
never moves. Every *will-based* countermeasure has failed; only *will-bypassing*
mechanical actuators (``mechanical_standdown``) have ever landed work. This
module removes the terminal push from the agent's will: the
loop, not a narrated sentence, commits+pushes the gate-clean working-tree edits
after the session.

:func:`land_uncommitted_edits` is a pure function of its injected seams — it has
no SDK session, no filesystem access of its own, and no place for a prose "done"
to stand in for the deed. It:

* (a) reads working-tree status via :attr:`LandSeams.has_edits`,
* (b) no-ops when there are no edits, or when *branch* is not an owned
  ``maestro/{task,evolve}/*`` branch (:func:`is_owned_landing_branch`),
* (c) runs the quality gates via :attr:`LandSeams.run_gates`, which returns an
  attribution-aware :class:`GateVerdict`,
* (d) on **GREEN** commits with a deterministic message
  (:data:`LAND_COMMIT_MESSAGE`) and — only if the injected oracle
  (:attr:`LandSeams.can_push`) permits — pushes via :attr:`LandSeams.push`,
  recording the typed :class:`PushResult` rather than letting a push failure
  raise,
* (e) on the branch's **own RED** rewinds the working tree via
  :attr:`LandSeams.rewind` and makes **zero** push calls,
* (f) on an **INHERITED RED** (the failure already present on the branch's
  base) holds the tree — no rewind, no commit, no push — so work the branch
  never broke is not destroyed,

returning a :class:`LandOutcome` that records exactly what happened. The oracle
seam is consulted *before* the raw push seam, so an oracle refusal makes zero
push calls (the human-in-the-loop / owned-branch push invariants can never be
widened by this path). Green-gated, rewind-on-own-red, hold-on-inherited-red,
typed push failure, owned+oracle-gated.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

#: Branch-name prefixes the actuator will land onto — the agent's own task and
#: evolution branches. Mirrors the shapes produced by
#: :func:`maestro.vcs.task_branch` / :func:`maestro.vcs.evolution_branch`. A
#: branch outside these prefixes is never committed/pushed by this path.
_OWNED_LANDING_PREFIXES: tuple[str, ...] = ("maestro/task/", "maestro/evolve/")

#: The deterministic commit subject for a mechanically-landed edit. Fixed so the
#: deed carries no narration — the loop's hand, not a composed sentence.
LAND_COMMIT_MESSAGE = "maestro: mechanically land gate-clean session edits"


class GateVerdict(StrEnum):
    """Attribution-aware result of the :attr:`LandSeams.run_gates` seam."""

    #: Every quality gate passed.
    GREEN = "green"
    #: The gates ran red and the red is the branch's own (or unattributable, or
    #: attribution is off) — the unverified edit is rewound.
    RED = "red"
    #: The gates ran red but the identical failure is already present on the
    #: branch's base (proposal 124 ``INHERITED``) — the tree is held.
    INHERITED_RED = "inherited_red"


class PushResult(StrEnum):
    """Typed result of the :attr:`LandSeams.push` seam — it never raises."""

    #: The push ran and the remote branch head was confirmed to advance.
    PUSHED = "pushed"
    #: The push failed (git error, rejection, or a late oracle refusal).
    FAILED = "failed"
    #: The push exited cleanly but the remote head was not seen to advance.
    UNCONFIRMED = "unconfirmed"


class LandStatus(StrEnum):
    """Terminal disposition of one :func:`land_uncommitted_edits` call."""

    #: *branch* is not an owned ``maestro/{task,evolve}/*`` branch — skipped
    #: before any gate run or push.
    NOT_OWNED = "not_owned"
    #: The working tree carried no edits — nothing to land.
    NO_EDITS = "no_edits"
    #: The quality gates ran RED; the working tree was rewound and nothing pushed.
    REWOUND = "rewound"
    #: The quality gates ran RED but the red is inherited from the base; the
    #: actuator held the working tree — nothing rewound, committed, or pushed
    #: by this path (the rest of the lap then treats the tree as flag-off).
    HELD = "held"
    #: Gates GREEN, committed, but the oracle refused the push — nothing pushed.
    PUSH_REFUSED = "push_refused"
    #: Gates GREEN, committed, oracle permitted, but the push failed — recorded,
    #: not raised; the commit stays local, ahead of origin.
    PUSH_FAILED = "push_failed"
    #: Gates GREEN, committed, pushed, but the remote head was not confirmed to
    #: advance — delivery unproven.
    PUSH_UNCONFIRMED = "push_unconfirmed"
    #: Gates GREEN, committed, oracle permitted, pushed, and the remote head was
    #: confirmed to advance — the head SHA moved.
    LANDED = "landed"


#: Dispositions that leave the session's work undelivered for a reason other
#: than a clean skip or an own-red rewind — the caller logs them at WARNING.
UNDELIVERED_STATUSES: frozenset[LandStatus] = frozenset({
    LandStatus.HELD,
    LandStatus.PUSH_REFUSED,
    LandStatus.PUSH_FAILED,
    LandStatus.PUSH_UNCONFIRMED,
})


@dataclass(frozen=True)
class LandOutcome:
    """What :func:`land_uncommitted_edits` did, as plain recorded facts.

    ``gates_ran`` is True only when the quality-gate runner was invoked (i.e.
    there were owned-branch edits to gate). ``committed`` is True once the
    commit seam ran (GREEN path). ``pushed`` is True only when the push seam
    ran after the oracle permitted it *and* reported :attr:`PushResult.PUSHED`
    (a confirmed remote-head advance) — the single fact that proves the head SHA
    moved.
    """

    status: LandStatus
    pushed: bool
    committed: bool
    gates_ran: bool


class _GateReview(Protocol):
    """The slice of a quality-gate result :func:`classify_gate_red` reads."""

    @property
    def passed(self) -> bool: ...


class _PushFlags(Protocol):
    """The slice of an owned-push result :func:`classify_push` reads."""

    @property
    def pushed(self) -> bool: ...

    @property
    def unconfirmed(self) -> bool: ...

    @property
    def error(self) -> str | None: ...


def classify_gate_red[R: _GateReview](
    review: R, attribute_red: Callable[[R], str | None],
) -> GateVerdict:
    """Map a gate run to a :class:`GateVerdict`, attributing only a red one.

    *attribute_red* is ``Agent._attribute_owned_gate_red`` (proposal 124), consulted
    only when *review* is red; ``"INHERITED"`` holds the tree, and every other
    reading — including ``None`` when observation is off/absent — keeps today's
    rewind.
    """
    if review.passed:
        return GateVerdict.GREEN
    if attribute_red(review) == "INHERITED":
        return GateVerdict.INHERITED_RED
    return GateVerdict.RED


def classify_push(result: _PushFlags) -> PushResult:
    """Map an owned-push outcome to a :class:`PushResult`.

    Anything neither confirmed-pushed nor unconfirmed (a git error, a rejection,
    or a late oracle refusal) is :attr:`PushResult.FAILED`.
    """
    if result.pushed:
        return PushResult.PUSHED
    if result.unconfirmed:
        return PushResult.UNCONFIRMED
    return PushResult.FAILED


def owned_push_seam[P: _PushFlags](
    push_owned: Callable[[str], P], errors: list[str],
) -> Callable[[str], PushResult]:
    """Adapt a non-raising owned push into the :attr:`LandSeams.push` seam.

    The git failure text of a failed push is appended to *errors* so the
    caller can log it — :class:`PushResult` itself carries no message.
    """

    def _push(branch: str) -> PushResult:
        result = push_owned(branch)
        if result.error:
            errors.append(result.error)
        return classify_push(result)

    return _push


def is_owned_landing_branch(branch: str) -> bool:
    """Return True when *branch* is an owned ``maestro/{task,evolve}/*`` branch.

    The actuator only ever writes to the agent's own task/evolution branches;
    any other ref (``main``, a foreign branch, an empty string) is refused
    before a gate runs or a push is attempted.
    """
    return any(branch.startswith(prefix) for prefix in _OWNED_LANDING_PREFIXES)


@dataclass(frozen=True)
class LandSeams:
    """Injected effects for :func:`land_uncommitted_edits` — pure by construction.

    Splitting the oracle (:attr:`can_push`) from the raw :attr:`push` is what
    makes "zero push calls when the oracle refuses" a literal, testable property
    of :func:`land_uncommitted_edits` rather than a promise buried inside a
    combined push seam.
    """

    #: Return True when the working tree carries uncommitted edits to land.
    has_edits: Callable[[], bool]
    #: Run the quality gates once and return the attribution-aware verdict.
    run_gates: Callable[[], GateVerdict]
    #: Stage and commit the working tree with the given deterministic message.
    commit: Callable[[str], None]
    #: Oracle: return True when *branch* is owned-writable and may be pushed.
    can_push: Callable[[str], bool]
    #: Push *branch* to origin — invoked only after ``can_push`` permits. Returns
    #: a typed :class:`PushResult`; a failure is reported, never raised.
    push: Callable[[str], PushResult]
    #: Discard the working-tree edits (rewind) on the branch's own RED result.
    rewind: Callable[[], None]


def land_uncommitted_edits(branch: str, seams: LandSeams) -> LandOutcome:
    """Mechanically commit+push gate-clean working-tree edits on *branch*.

    Pure — every effect is one of *seams*' injected callables, so the whole
    control flow is unit-testable with plain fakes (no VCS, no SDK, no network).

    The order of decisions is load-bearing:

    1. A non-owned *branch* is skipped with **no** gate run and **no** push
       (:attr:`LandStatus.NOT_OWNED`).
    2. An edit-free working tree is a no-op with **no** gate run and **no** push
       (:attr:`LandStatus.NO_EDITS`).
    3. An INHERITED RED holds the working tree — **no** rewind, commit, or push
       (:attr:`LandStatus.HELD`).
    4. The branch's own RED rewinds the working tree and pushes **nothing**
       (:attr:`LandStatus.REWOUND`).
    5. On GREEN the commit seam runs; then the oracle is consulted — a refusal
       makes **zero** push calls (:attr:`LandStatus.PUSH_REFUSED`); a permit runs
       the push, whose typed result decides the status: a confirmed advance
       (:attr:`LandStatus.LANDED`), a failure (:attr:`LandStatus.PUSH_FAILED`),
       or an unconfirmed advance (:attr:`LandStatus.PUSH_UNCONFIRMED`).
    """
    if not is_owned_landing_branch(branch):
        return LandOutcome(
            status=LandStatus.NOT_OWNED,
            pushed=False,
            committed=False,
            gates_ran=False,
        )
    if not seams.has_edits():
        return LandOutcome(
            status=LandStatus.NO_EDITS,
            pushed=False,
            committed=False,
            gates_ran=False,
        )
    verdict = seams.run_gates()
    if verdict is GateVerdict.INHERITED_RED:
        # INHERITED: the red belongs to the base, not this edit — hold the tree
        # rather than destroy work the branch never broke.
        return LandOutcome(
            status=LandStatus.HELD,
            pushed=False,
            committed=False,
            gates_ran=True,
        )
    if verdict is GateVerdict.RED:
        # RED: never ship an unverified edit — rewind and push nothing.
        seams.rewind()
        return LandOutcome(
            status=LandStatus.REWOUND,
            pushed=False,
            committed=False,
            gates_ran=True,
        )
    # GREEN: the edits are gate-clean, so commit them and — only if the oracle
    # permits — push, moving the head SHA by the loop's hand.
    seams.commit(LAND_COMMIT_MESSAGE)
    if not seams.can_push(branch):
        return LandOutcome(
            status=LandStatus.PUSH_REFUSED,
            pushed=False,
            committed=True,
            gates_ran=True,
        )
    result = seams.push(branch)
    if result is PushResult.PUSHED:
        status = LandStatus.LANDED
    elif result is PushResult.UNCONFIRMED:
        status = LandStatus.PUSH_UNCONFIRMED
    else:
        status = LandStatus.PUSH_FAILED
    return LandOutcome(
        status=status,
        pushed=result is PushResult.PUSHED,
        committed=True,
        gates_ran=True,
    )
