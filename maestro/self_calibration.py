"""Self-calibration oracle — the pure owner deciding whether a reflection's named
loss condition actually occurred.

Every priced reflection ends with two mandated lines: a ``PREDICTION:`` (a
falsifiable claim about next-cycle behaviour) and a ``LOSS CONDITION:`` (the
observable outcome that would prove it wrong). Both are persisted to the
prediction ledger, and the next cycle re-injects the bet and asks the model to
grade it. The loop *looks* closed but is not: the loss-condition string is stored
as free text no code reads, and the verdict folded into
:func:`~maestro.audit_checks_calibration.measure_prediction_accuracy` is the predictor's own
judgment of its own bet — a self-report about a self-report. The one part of the
ritual that was meant to be *loseable* — the named loss condition — has never
once been mechanically evaluated.

This module is the closed, provenance-stamped computer of *"did the named loss
condition actually occur?"*. It defines a closed probe vocabulary
(:data:`ProbeKind`), a :func:`parse_loss_condition` that turns a loss-condition
string into a typed :class:`LossProbe` (or declines, marking the bet
``unfalsifiable``), and a :func:`score_loss_condition` that decides the bet's
verdict from *injected* observations — with full temporal semantics, so a bet
whose window has not closed scores ``pending`` rather than being graded early,
and a fact still unobservable at its cutoff scores ``unresolved`` rather than
being silently counted as a loss.

Its pure functions stay I/O-free — the caller injects every observation, so this
owner never touches the filesystem. Proposal 129 wires the *input* half of the
loop: :data:`LOSS_CONDITION_GRAMMAR` is the closed vocabulary the priced
reflection prompt teaches from behind ``[audit] self_calibration_enabled``, and
``agent.contemplate`` records the :class:`ProbeKind` each stated loss condition
parses to. The *scoring* half — calling :func:`score_loss_condition` at maturity
and rebinding the ledger verdict from the ``PRIOR VERDICT:`` self-grade to this
oracle — is authored by :func:`author_reflection_verdict` behind
``[audit] calibration_verdict_authority_enabled``: ``agent.contemplate`` scores
each open bet and hands the :class:`BetScore` here, the measured verdict is
adopted with the self-grade retained beside it as narration, and every
measured-vs-narrated divergence is stamped to the gate-activation trace; with the
flag off the self-grade passes through unchanged, byte-identical to before.
Imports only from :mod:`maestro.types` — never from
:mod:`maestro.audit_checks` — so the later change that folds these verdicts into
the prediction-accuracy aggregate can import this module without a cycle. Mirrors
the :mod:`maestro.frame_discharge` and :mod:`maestro.plan_grounding` pure-owner
skeleton — a ``Literal`` closed vocabulary, ``@dataclass(frozen=True)``
provenance-carrying value objects, and plain module-level pure ``def``s.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal, get_args

from maestro.types import PlanStatus, TaskOutcome

#: The closed probe vocabulary — one kind per family of loss condition the
#: grammar can express. ``TASK_OUTCOME``: a task of a named shape reaches a named
#: outcome; ``GATE_FIRING``: a named gate fires; ``PR_SETTLEMENT``: a numbered PR
#: settles; ``PLAN_STATUS``: a plan reaches a named status; ``SENSOR_THRESHOLD``:
#: a named behavioral sensor reads above/below a threshold. Kept a ``Literal`` so
#: a new kind is a one-line vocabulary edit.
ProbeKind = Literal[
    "TASK_OUTCOME", "GATE_FIRING", "PR_SETTLEMENT", "PLAN_STATUS", "SENSOR_THRESHOLD"
]

#: The two polarities of a loss condition. ``OCCURS`` — the bet *loses* when the
#: named event happens ("PR #123 settles" as a loss condition). ``DOES_NOT_OCCUR``
#: — the bet loses when the named event *fails* to happen within the window ("PR
#: #123 does not settle"). Selected by the negated variant of each grammar line.
Polarity = Literal["OCCURS", "DOES_NOT_OCCUR"]

#: The closed verdict vocabulary — the whole range :func:`score_loss_condition`
#: can return. ``correct`` / ``wrong``: the bet resolved. ``pending``: the window
#: has not matured, so it is too early to grade. ``unresolved``: the fact was
#: still unobservable at the cutoff, so the bet is abandoned rather than counted
#: as a loss. ``unfalsifiable``: the loss condition could not be parsed into a
#: probe at all — a bet the grammar cannot score.
CalibrationVerdict = Literal[
    "correct", "wrong", "pending", "unresolved", "unfalsifiable"
]

#: The closed set of ``reason`` slugs :func:`score_loss_condition` stamps onto a
#: :class:`BetScore` — one per branch it can take. Kept a ``Literal`` (like every
#: other vocabulary here) so the set is enforceable and a later reporting change
#: can switch on it exhaustively; every ``reason=`` literal in this module is a
#: member of this set.
BetReason = Literal[
    "no-parseable-probe",
    "unobservable-pending",
    "unobservable-at-cutoff",
    "awaiting-maturity",
    "held-through-window",
    "loss-condition-occurred",
    "loss-averted-by-event",
    "loss-realized-by-absence",
]

#: The task shapes a ``TASK_OUTCOME`` probe may name, a module-local copy of the
#: live shape vocabulary (:data:`maestro.audit_checks.TaskShape`). Copied rather
#: than imported so this owner stays free of an ``audit_checks`` import (see the
#: module docstring); ``tests/test_self_calibration.py`` pins this tuple to
#: ``get_args(audit_checks.TaskShape)`` so the local copy cannot silently rot.
_TASK_TARGET_SHAPES: tuple[str, ...] = (
    "github_issue",
    "branch_promotion",
    "module_extension",
    "measurement",
    "remediation",
    "external_request",
    "construction",
    "investigation",
    "other",
)

#: Valid ``TASK_OUTCOME`` outcome operands and ``PLAN_STATUS`` status operands,
#: derived from the canonical literals so a token the grammar accepts is always a
#: real outcome/status. Derived at import via ``get_args`` so adding a value to
#: the literal picks it up here automatically.
_VALID_OUTCOMES: frozenset[str] = frozenset(get_args(TaskOutcome))
_VALID_STATUSES: frozenset[str] = frozenset(get_args(PlanStatus))

#: A bet more than this many cycles out is an unloseably-distant horizon, not a
#: real bet — :func:`parse_loss_condition` declines a window above it (returning
#: ``None`` so the bet scores ``unfalsifiable``).
_MAX_WINDOW_CYCLES = 50

#: Digit-width bound on every numeric capture in the family grammar. Purely an
#: int-safety bound, not a semantic one: CPython refuses an ``int()`` conversion
#: of a digit run longer than ``sys.get_int_max_str_digits()`` (4300 by default)
#: by *raising*, which would break the parser's never-raise contract on
#: model-authored text. Bounding the capture instead makes an oversized token
#: simply not match, so the bet declines to ``None`` on the existing path. It is
#: deliberately far above every legitimate value (windows are capped at
#: :data:`_MAX_WINDOW_CYCLES`, PR numbers are five or six digits) so the
#: *semantic* guards, not this one, do the deciding.
_MAX_NUMERIC_TOKEN_DIGITS = 9

#: The gap between a window's close and its cutoff. The close
#: (:attr:`ObservationWindow.closes_at_elapsed`, ``N + 1``) is when a
#: *non-occurrence* becomes a settled verdict; cutoff is the later point at which
#: a fact still *unobservable* (the caller could not read it) is abandoned as
#: ``unresolved``. The two are distinct: an event that provably did not happen is
#: gradeable at the close, but a fact we simply could not measure deserves a grace
#: window before we give up on ever observing it.
_CUTOFF_GRACE_CYCLES = 2

#: The furthest back a bet can still resolve to anything but ``unresolved``: a
#: window is at most :data:`_MAX_WINDOW_CYCLES`, and an unobservable fact gets
#: :data:`_CUTOFF_GRACE_CYCLES` of grace past it before being abandoned. Exported
#: so the ``agent.contemplate`` caller can bound how far back it re-scores when the
#: authority flag is first enabled — a bet older than this can only score
#: ``unresolved``, so re-scoring ancient pre-flag bets would only flood the ledger.
#: It is the widest window's cutoff: ``N + 1`` (the window closes once iteration
#: ``c + N + 1`` begins) plus the grace.
MAX_SCOREABLE_AGE_CYCLES = _MAX_WINDOW_CYCLES + 1 + _CUTOFF_GRACE_CYCLES


def max_scoreable_age(contemplate_every: int) -> int:
    """The oldest bet age the contemplate caller still scores, for its cadence.

    Bets are scored only on contemplate iterations (every *contemplate_every*
    iterations), so the first scoring pass at or after the widest cutoff can land
    up to ``contemplate_every - 1`` iterations past :data:`MAX_SCOREABLE_AGE_CYCLES`;
    widening by that much keeps a matured pass reachable instead of stranding the
    bet ``pending`` forever.
    """
    return MAX_SCOREABLE_AGE_CYCLES + max(0, contemplate_every - 1)


@dataclass(frozen=True)
class ObservationWindow:
    """The two temporal thresholds a bet is scored against.

    ``matures_after_cycles`` is the stated window ``N``; a *non-occurrence*
    settles into a verdict only once the window has closed, at
    :attr:`closes_at_elapsed` (``N + 1``) — before it, an unobserved ``OCCURS``
    loss condition is still ``pending``. ``cutoff_after_cycles`` (always later
    than the close, by :data:`_CUTOFF_GRACE_CYCLES`) is when a fact that stayed *unobservable* is abandoned as ``unresolved``
    rather than counted as anything. Frozen so it is a stable provenance record on
    the probe.
    """

    matures_after_cycles: int
    cutoff_after_cycles: int

    @property
    def closes_at_elapsed(self) -> int:
        """The elapsed count at which the window has closed — the sole owner.

        A bet placed at iteration ``c`` with an ``N``-cycle window counts iteration
        ``c + N`` as fully inside it, so the window is closed only once iteration
        ``c + N + 1`` begins. Both the scorer (a non-occurrence scores final only
        here) and the evidence bound (``calibration_window_ends``) read this.
        """
        return self.matures_after_cycles + 1


@dataclass(frozen=True)
class LossProbe:
    """A parsed, typed loss condition — the single check a bet is scored against.

    ``kind`` selects the family. ``target`` is the named entity (task shape / gate
    name / PR number / plan id / sensor tag) and ``operand`` its second value where
    one exists (the outcome for ``TASK_OUTCOME``, the status for ``PLAN_STATUS``, a
    ``"<above|below> <number>"`` comparison for ``SENSOR_THRESHOLD``; empty for
    ``GATE_FIRING`` / ``PR_SETTLEMENT``). ``polarity`` records whether the bet
    loses when the event *occurs* or when it *does not*. Frozen so it is a stable
    premise-verification provenance record.
    """

    kind: ProbeKind
    target: str
    operand: str
    polarity: Polarity
    window: ObservationWindow


@dataclass(frozen=True)
class CalibrationEvidence:
    """The caller-supplied observation bundle a probe is scored against.

    Every field is a tuple with an empty default so a caller supplies only what it
    can actually see. The two ``*_covered`` booleans exist because, for task
    history and the gate ledger, *"the caller read the window and found nothing"*
    is genuine negative evidence, while *"the caller supplied nothing"* is not —
    and an empty tuple alone cannot tell the two apart. So a ``TASK_OUTCOME`` /
    ``GATE_FIRING`` probe is negative-capable (can resolve to *did not happen*)
    only when its ``*_covered`` flag is set; otherwise an absent match resolves to
    *cannot observe*. ``settled_prs`` carries no covered flag on purpose:
    settlement is positive evidence only — a PR absent from the set is always
    *cannot observe*, never *did not settle* (see :func:`score_loss_condition`).

    **Window scoping — every field is window-of-transitions, not a snapshot.** The
    caller must inject every value observed *at any point within the bet's window*,
    not just the value current at scoring time. This matters for the transition
    verbs the grammar uses ("reaches", "fires", "settles"): a ``PLAN_STATUS`` probe
    for ``reaches implemented`` is satisfied by a plan that *passed through*
    ``implemented`` during the window even if it has since moved on to ``merged``,
    so the caller must supply the ``("p-1", "implemented")`` row it saw, not only
    the plan's current status. ``fired_gates`` and ``task_outcomes`` are the union
    of what fired / completed across the window; ``sensor_readings`` are the
    per-cycle readings taken within it. A caller that injects only the current
    snapshot would under-report transitions and mis-score these bets.

    **PR settlement is one-directional by design.** Because ``settled_prs`` is
    positive evidence only, a ``pr #N does not settle`` (``DOES_NOT_OCCUR``) bet can
    only ever score ``correct`` / ``pending`` / ``unresolved`` — never ``wrong`` —
    and a ``pr #N settles`` (``OCCURS``) bet can only score ``wrong`` / ``pending``
    / ``unresolved`` — never ``correct``. This asymmetry is deliberate: an unsettled
    PR is genuinely unobservable (we cannot prove a PR will *never* settle), so the
    grammar refuses to manufacture a negative it cannot ground, accepting that the
    non-loss direction of a PR bet is not mechanically graded (see the module
    docstring's positive-evidence-only note and :func:`score_loss_condition`).
    """

    task_outcomes: tuple[tuple[str, str], ...] = ()
    task_window_covered: bool = False
    fired_gates: tuple[str, ...] = ()
    gate_window_covered: bool = False
    settled_prs: tuple[int, ...] = ()
    plan_statuses: tuple[tuple[str, str], ...] = ()
    sensor_readings: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class BetScore:
    """The scored verdict plus the provenance it was computed from.

    ``verdict`` is the :data:`CalibrationVerdict`. ``kind`` carries the probe kind
    the verdict was derived from — ``None`` only for an ``unfalsifiable`` bet,
    which never had a probe. ``observed`` is the resolved observation the verdict
    turned on (``True`` = the named event was seen, ``False`` = it provably was
    not, ``None`` = it could not be observed / no probe). ``reason`` is a short
    fixed slug (the closed :data:`BetReason` set) naming *which* branch fired, so a
    later reader can report why a bet scored as it did without re-deriving it.
    """

    verdict: CalibrationVerdict
    kind: ProbeKind | None
    observed: bool | None
    reason: BetReason


# ── Grammar: one anchored regex per probe family, case-insensitive ──────────────
# Canonical line shape: ``<keyword> <target> [relation] within <N> cycles``, with
# the negated variant selecting ``DOES_NOT_OCCUR``. Anchored ``^…$`` so a partial
# match never grabs a probe out of surrounding prose; whitespace-tolerant and a
# trailing period allowed. Each ``_parse_*`` returns ``None`` on any validation
# failure rather than raising — an unparseable bet is scored ``unfalsifiable``.
# Every numeric capture is width-bounded by ``_MAX_NUMERIC_TOKEN_DIGITS`` so the
# ``int()`` conversions downstream of a match cannot raise on an oversized
# model-authored digit run; that is what keeps the never-raise contract true.

#: The bounded digit-run fragment every numeric capture in the grammar is built
#: from, derived from :data:`_MAX_NUMERIC_TOKEN_DIGITS` so the two cannot drift.
_DIGITS = rf"\d{{1,{_MAX_NUMERIC_TOKEN_DIGITS}}}"

_TASK_RE = re.compile(
    r"^task\s+(?P<shape>\S+)\s+outcome\s+is\s+(?P<neg>not\s+)?(?P<outcome>\S+)"
    rf"\s+within\s+(?P<n>{_DIGITS})\s+cycles?\.?$",
    re.IGNORECASE,
)
_GATE_RE = re.compile(
    r"^gate\s+(?P<name>\S+)\s+(?P<neg>does\s+not\s+)?fires?"
    rf"\s+within\s+(?P<n>{_DIGITS})\s+cycles?\.?$",
    re.IGNORECASE,
)
_PR_RE = re.compile(
    rf"^pr\s+#?(?P<pr>{_DIGITS})\s+(?P<neg>does\s+not\s+)?settles?"
    rf"\s+within\s+(?P<n>{_DIGITS})\s+cycles?\.?$",
    re.IGNORECASE,
)
_PLAN_RE = re.compile(
    r"^plan\s+(?P<id>\S+)\s+(?P<neg>does\s+not\s+)?reach(?:es)?\s+(?P<status>\S+)"
    rf"\s+within\s+(?P<n>{_DIGITS})\s+cycles?\.?$",
    re.IGNORECASE,
)
_SENSOR_RE = re.compile(
    r"^sensor\s+(?P<tag>\S+)\s+(?P<neg>does\s+not\s+)?go(?:es)?"
    r"\s+(?P<dir>above|below)\s+(?P<num>-?\d+(?:\.\d+)?)"
    rf"\s+within\s+(?P<n>{_DIGITS})\s+cycles?\.?$",
    re.IGNORECASE,
)

#: One canonical loss-condition line per :data:`ProbeKind`, in :data:`ProbeKind`
#: order — the vocabulary the reflection prompt teaches from. Declared beside the
#: family regexes so the oracle stays the single owner of its own grammar: the
#: prompt quotes this tuple rather than restating shapes that would silently
#: drift from the parser, and ``tests/test_self_calibration.py`` pins every entry
#: to the :class:`LossProbe` its family produces and the tuple to
#: ``get_args(ProbeKind)`` exactly, so an example the parser rejects reds the
#: build. Each line is stated *bare* — without a ``LOSS CONDITION:`` prefix — so a
#: model echoing an example mid-reflection cannot shadow the genuine trailing
#: marker line that :func:`maestro.prompts.reflection._marker_value` reads (it
#: takes the last matching line).
LOSS_CONDITION_GRAMMAR: tuple[str, ...] = (
    "task remediation outcome is not success within 3 cycles",
    "gate wip_primacy does not fire within 5 cycles",
    "pr #3725 settles within 10 cycles",
    "plan p-42 reaches implemented within 5 cycles",
    "sensor FORCED-QUIET-RATIO goes above 0.5 within 4 cycles",
)


def _window_or_none(n_text: str) -> ObservationWindow | None:
    """Build the two-point window for a parsed cycle count, or ``None`` if invalid.

    A non-positive count or one above :data:`_MAX_WINDOW_CYCLES` is not a bet, so
    it declines to ``None`` (the caller returns ``None``, scoring the bet
    ``unfalsifiable``). ``matures_after_cycles`` is the stated ``N``;
    ``cutoff_after_cycles`` is ``N + 1 + _CUTOFF_GRACE_CYCLES`` — the grace is
    measured from the window's close (:attr:`ObservationWindow.closes_at_elapsed`).

    The ``int()`` below is safe because the only caller is a family parser acting
    on a grammar match, and every numeric capture is width-bounded by
    :data:`_MAX_NUMERIC_TOKEN_DIGITS` — far under CPython's int-conversion digit
    limit, which would otherwise *raise* on a model-authored digit run and break
    this module's never-raise contract. The guard below restates that bound so a
    future grammar edit that reopened the hole fails loudly and locally here
    rather than as a ``ValueError`` escaping into the contemplation phase.
    """
    if len(n_text) > _MAX_NUMERIC_TOKEN_DIGITS:
        return None
    n = int(n_text)
    if not 1 <= n <= _MAX_WINDOW_CYCLES:
        return None
    return ObservationWindow(
        matures_after_cycles=n, cutoff_after_cycles=n + 1 + _CUTOFF_GRACE_CYCLES
    )


def _polarity(neg_group: str | None) -> Polarity:
    """Map a regex negation group to a polarity: present ⇒ ``DOES_NOT_OCCUR``."""
    return "DOES_NOT_OCCUR" if neg_group else "OCCURS"


def _parse_task(text: str) -> LossProbe | None:
    """Parse a ``task <shape> outcome is [not] <outcome> within <N> cycles`` bet.

    Declines (``None``) when the shape is not a live task shape
    (:data:`_TASK_TARGET_SHAPES`), the outcome is not a real
    :data:`~maestro.types.TaskOutcome`, or the window is invalid.
    """
    m = _TASK_RE.match(text)
    if m is None:
        return None
    shape = m.group("shape").lower()
    outcome = m.group("outcome").lower()
    if shape not in _TASK_TARGET_SHAPES or outcome not in _VALID_OUTCOMES:
        return None
    window = _window_or_none(m.group("n"))
    if window is None:
        return None
    return LossProbe(
        kind="TASK_OUTCOME",
        target=shape,
        operand=outcome,
        polarity=_polarity(m.group("neg")),
        window=window,
    )


def _parse_gate(text: str) -> LossProbe | None:
    """Parse a ``gate <name> [does not] fire[s] within <N> cycles`` bet."""
    m = _GATE_RE.match(text)
    if m is None:
        return None
    window = _window_or_none(m.group("n"))
    if window is None:
        return None
    return LossProbe(
        kind="GATE_FIRING",
        target=m.group("name"),
        operand="",
        polarity=_polarity(m.group("neg")),
        window=window,
    )


def _parse_pr(text: str) -> LossProbe | None:
    """Parse a ``pr #<NNNN> [does not] settle[s] within <N> cycles`` bet."""
    m = _PR_RE.match(text)
    if m is None:
        return None
    window = _window_or_none(m.group("n"))
    if window is None:
        return None
    return LossProbe(
        kind="PR_SETTLEMENT",
        target=m.group("pr"),
        operand="",
        polarity=_polarity(m.group("neg")),
        window=window,
    )


def _parse_plan(text: str) -> LossProbe | None:
    """Parse a ``plan <id> [does not] reach[es] <status> within <N> cycles`` bet.

    Declines (``None``) when the status is not a real
    :data:`~maestro.types.PlanStatus` or the window is invalid.
    """
    m = _PLAN_RE.match(text)
    if m is None:
        return None
    status = m.group("status").lower()
    if status not in _VALID_STATUSES:
        return None
    window = _window_or_none(m.group("n"))
    if window is None:
        return None
    return LossProbe(
        kind="PLAN_STATUS",
        target=m.group("id"),
        operand=status,
        polarity=_polarity(m.group("neg")),
        window=window,
    )


def _parse_sensor(text: str) -> LossProbe | None:
    """Parse a ``sensor <TAG> [does not] go[es] above|below <number> within <N> cycles`` bet.

    The operand is stored canonically as ``"<above|below> <number>"`` (the float
    normalised by :class:`float`), which :func:`score_loss_condition` splits back
    apart — the regex guarantees both a valid direction and a valid number, so
    that split never needs to guard against malformed input.
    """
    m = _SENSOR_RE.match(text)
    if m is None:
        return None
    window = _window_or_none(m.group("n"))
    if window is None:
        return None
    direction = m.group("dir").lower()
    threshold = float(m.group("num"))
    return LossProbe(
        kind="SENSOR_THRESHOLD",
        target=m.group("tag"),
        operand=f"{direction} {threshold}",
        polarity=_polarity(m.group("neg")),
        window=window,
    )


def parse_loss_condition(text: str) -> LossProbe | None:
    """Parse a loss-condition string into a typed :class:`LossProbe`, or ``None``.

    *text* is the already-extracted ``LOSS CONDITION:`` value
    (:func:`maestro.prompts.reflection.parse_reflection_prediction` yields it).
    Each probe family is tried in turn; because every family regex is anchored and
    keyword-prefixed, at most one matches. Returns ``None`` — never raises — on any
    unmatched keyword, unknown token, missing/malformed/out-of-range window, or
    empty input, so a bet the grammar cannot express is scored ``unfalsifiable``
    rather than crashing the loop.
    """
    stripped = text.strip()
    if not stripped:
        return None
    for parser in (_parse_task, _parse_gate, _parse_pr, _parse_plan, _parse_sensor):
        probe = parser(stripped)
        if probe is not None:
            return probe
    return None


def _resolve_observation(
    probe: LossProbe, evidence: CalibrationEvidence
) -> bool | None:
    """Resolve the probe's named event to ``True`` / ``False`` / ``None`` (cannot observe).

    Per kind: a ``TASK_OUTCOME`` / ``GATE_FIRING`` match is ``True``; an absent
    match is ``False`` only when the corresponding ``*_covered`` flag says the
    caller read the window (genuine negative evidence), else ``None``. A
    ``PR_SETTLEMENT`` in ``settled_prs`` is ``True``; a PR *absent* is always
    ``None``, never ``False`` — settlement is positive evidence only. A
    ``PLAN_STATUS`` whose id+status match is ``True``; an id present with a
    different status is ``False``; an absent id is ``None``. A ``SENSOR_THRESHOLD``
    is ``True`` if *any* reading for the tag crosses (``above`` ⇒ ``>``, ``below``
    ⇒ ``<``), ``False`` if at least one reading matched the tag and none crossed,
    and ``None`` if the tag was never seen — an any-match scan, so the verdict does
    not depend on reading order.

    Gate names and sensor tags are matched case-insensitively: the family regexes
    are ``re.IGNORECASE`` and preserve the operand's original case in the probe, so
    a case variant ("WIP_Primacy" vs the lowercase ``fired_gates`` entry) must not
    read as a confident negative.
    """
    if probe.kind == "TASK_OUTCOME":
        for shape, outcome in evidence.task_outcomes:
            if shape == probe.target and outcome == probe.operand:
                return True
        return False if evidence.task_window_covered else None
    if probe.kind == "GATE_FIRING":
        fired_lower = {fired.lower() for fired in evidence.fired_gates}
        if probe.target.lower() in fired_lower:
            return True
        return False if evidence.gate_window_covered else None
    if probe.kind == "PR_SETTLEMENT":
        # Settlement is positive evidence only: "not in the set" must never be
        # read as "did not settle", so an absent PR is unobservable, not False.
        # The conversion is safe because the grammar width-bounds the PR capture
        # (:data:`_MAX_NUMERIC_TOKEN_DIGITS`); an out-of-bound target can only
        # reach here via a hand-built probe, and is unobservable rather than a
        # ``ValueError`` out of the oracle. ``isascii()`` carries its weight:
        # ``str.isdigit()`` is True for superscripts like "²" that ``int()``
        # still rejects, so digit-ness alone would not make the conversion safe.
        if (
            not probe.target.isascii()
            or not probe.target.isdigit()
            or len(probe.target) > _MAX_NUMERIC_TOKEN_DIGITS
        ):
            return None
        if int(probe.target) in evidence.settled_prs:
            return True
        return None
    if probe.kind == "PLAN_STATUS":
        present = False
        for plan_id, status in evidence.plan_statuses:
            if plan_id != probe.target:
                continue
            present = True
            if status == probe.operand:
                return True
        return False if present else None
    # SENSOR_THRESHOLD is the only remaining kind. Scan *all* matching readings
    # (any-match, like the sibling families): True if any crosses, False only when
    # at least one reading matched the tag and none crossed, None when the tag was
    # never seen. Tag comparison is case-insensitive (see the docstring).
    target = probe.target.lower()
    direction, _, number = probe.operand.partition(" ")
    threshold = float(number)
    matched = False
    for tag, value in evidence.sensor_readings:
        if tag.lower() != target:
            continue
        matched = True
        if direction == "above" and value > threshold:
            return True
        if direction == "below" and value < threshold:
            return True
    return False if matched else None


def score_loss_condition(
    probe: LossProbe | None,
    evidence: CalibrationEvidence,
    *,
    cycles_elapsed: int,
) -> BetScore:
    """Score a bet's loss condition against injected *evidence* at *cycles_elapsed*.

    A ``None`` *probe* is ``unfalsifiable`` — the grammar could not express the
    bet. Otherwise the named event is resolved to observed / not-observed /
    unobservable (:func:`_resolve_observation`), then mapped to a verdict, keeping
    in mind the probe describes the condition under which the bet *loses*:

    * unobservable (``None``): ``pending`` before the cutoff, ``unresolved`` at or
      past it — a fact we could not measure is abandoned, never counted as a loss.
    * ``OCCURS`` loss condition: observed ⇒ ``wrong``; unobserved while the window
      is open ⇒ ``pending``; unobserved once the window has closed
      (:attr:`ObservationWindow.closes_at_elapsed`) ⇒ ``correct``.
    * ``DOES_NOT_OCCUR`` loss condition: observed ⇒ ``correct`` (the loss can no
      longer happen); unobserved while the window is open ⇒ ``pending``;
      unobserved once the window has closed ⇒ ``wrong``.

    Pure, deterministic, no I/O — the caller supplies every observation.
    """
    if probe is None:
        return BetScore(
            verdict="unfalsifiable",
            kind=None,
            observed=None,
            reason="no-parseable-probe",
        )
    observed = _resolve_observation(probe, evidence)
    window = probe.window
    if observed is None:
        if cycles_elapsed >= window.cutoff_after_cycles:
            return BetScore(
                verdict="unresolved",
                kind=probe.kind,
                observed=None,
                reason="unobservable-at-cutoff",
            )
        return BetScore(
            verdict="pending",
            kind=probe.kind,
            observed=None,
            reason="unobservable-pending",
        )
    matured = cycles_elapsed >= window.closes_at_elapsed
    if probe.polarity == "OCCURS":
        if observed:
            return BetScore(
                verdict="wrong",
                kind=probe.kind,
                observed=True,
                reason="loss-condition-occurred",
            )
        if matured:
            return BetScore(
                verdict="correct",
                kind=probe.kind,
                observed=False,
                reason="held-through-window",
            )
        return BetScore(
            verdict="pending",
            kind=probe.kind,
            observed=False,
            reason="awaiting-maturity",
        )
    if observed:
        return BetScore(
            verdict="correct",
            kind=probe.kind,
            observed=True,
            reason="loss-averted-by-event",
        )
    if matured:
        return BetScore(
            verdict="wrong",
            kind=probe.kind,
            observed=False,
            reason="loss-realized-by-absence",
        )
    return BetScore(
        verdict="pending",
        kind=probe.kind,
        observed=False,
        reason="awaiting-maturity",
    )


@dataclass(frozen=True)
class AuthoredVerdict:
    """The verdict the reflection ledger records, plus where it came from.

    ``verdict`` is the string written to the ``reflection_verdict`` row.
    ``source`` is ``"measured"`` when the oracle's :func:`score_loss_condition`
    decided it, ``"narrated"`` when the grammar could not express the bet and the
    model's ``PRIOR VERDICT:`` self-grade was adopted instead, and ``None`` on the
    flag-off path — where the self-grade is passed through unchanged and *no*
    source is stamped, so the persisted row stays byte-identical to the pre-oracle
    write. ``self_verdict`` carries the model's self-grade *beside* a measured
    verdict, retained as narration so a measured-vs-narrated divergence stays
    visible; it is ``None`` on the narrated / flag-off paths (there the self-grade
    already *is* the verdict) and when no self-grade was stated. Frozen so it is a
    stable authorship record.
    """

    verdict: str
    source: Literal["measured", "narrated"] | None
    self_verdict: str | None


def author_reflection_verdict(
    score: BetScore | None,
    self_grade: str | None,
) -> AuthoredVerdict | None:
    """Decide what verdict the reflection ledger records — the sole author.

    The measurement is authoritative when it exists: a *score* whose verdict is
    ``correct`` / ``wrong`` / ``pending`` / ``unresolved`` is adopted as
    ``"measured"``, with *self_grade* carried alongside as narration (or ``None``
    when none was stated). An ``unfalsifiable`` score — the grammar could not
    express the bet — falls back to the self-grade stamped ``"narrated"``, so the
    trace can tell a narration from a measurement. A ``None`` *score* is the
    flag-off call: the self-grade is adopted with ``source=None`` and no narration
    field, so the caller writes a row byte-identical to today's. In every fallback
    an empty self-grade leaves nothing to record and returns ``None``. Pure and
    I/O-free.
    """
    if score is not None and score.verdict != "unfalsifiable":
        return AuthoredVerdict(
            verdict=score.verdict,
            source="measured",
            self_verdict=self_grade or None,
        )
    if not self_grade:
        return None
    source: Literal["measured", "narrated"] | None = (
        "narrated" if score is not None else None
    )
    return AuthoredVerdict(verdict=self_grade, source=source, self_verdict=None)


def _normalize_loss_condition(text: str) -> str:
    """Collapse whitespace and lowercase a loss-condition string for repeat-matching.

    The comparison key the stale-repeat guard groups on: internal whitespace runs
    collapse to a single space, leading/trailing whitespace is stripped, and the
    result is lowercased. Deliberately narrow — matching on this key curbs a
    *verbatim* (modulo whitespace/case) treadmill, not a reworded restatement of
    the same unownable bet.
    """
    return " ".join(text.split()).lower()


def stale_repeat_loss_conditions(
    prediction_rows: Iterable[Mapping[str, object]],
    *,
    threshold: int = 2,
) -> set[str]:
    """Return the normalised loss conditions that repeat without ever scoring correct.

    A loss-condition string is *stale* when it appears in at least *threshold*
    prior prediction rows and **none** of those rows scored ``"correct"`` — the
    "comfort bet" signature: an always-winnable / never-scoring wager re-run
    verbatim as a hiding place. Each *prediction_rows* entry is a mapping carrying
    at least a ``loss_condition`` and its effective ``verdict`` (the reflection
    readers' joined shape). A non-string ``loss_condition``, or one that normalises
    to empty, is skipped — a blank or placeholder bet is never banned. The count
    and the "ever correct" test both key on :func:`_normalize_loss_condition`
    (whitespace-collapsed, lowercased), so a *single* ``correct`` scoring of a
    string excludes it from the ban regardless of its repeat count, and a reworded
    restatement of the same bet is deliberately *not* caught — this owner curbs the
    verbatim treadmill only.

    Pure and I/O-free: the caller supplies every row.
    """
    counts: dict[str, int] = {}
    ever_correct: set[str] = set()
    for row in prediction_rows:
        raw = row.get("loss_condition")
        if not isinstance(raw, str):
            continue
        key = _normalize_loss_condition(raw)
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
        verdict = row.get("verdict")
        if isinstance(verdict, str) and verdict == "correct":
            ever_correct.add(key)
    return {
        key
        for key, count in counts.items()
        if count >= threshold and key not in ever_correct
    }


# ── Claim citation grammar (proposal 129 evidence-bound self-knowledge) ──────────
# The sibling half of the loss-condition grammar above, for the *other* self-model
# citation: a ``capabilities_known`` claim's evidence anchor. Where the loss
# condition names a future event a bet loses on, a claim citation names something
# that *already happened* — a task that resolved ``success``, a merge SHA in the
# version archive, or a behavioural-sensor tag the loop actually emits. The
# resolver decides existence-plus-polarity (not entailment): it separates a claim
# anchored to a real positive outcome from one anchored to nothing, to a task that
# failed, or to a tag no sensor emits. Like the loss-condition owner above it is
# pure and I/O-free — the caller injects every observation via
# :class:`ClaimEvidenceSources`, mirroring how :class:`CalibrationEvidence` takes
# every observation from its caller, because ``self_calibration → memory`` is a
# genuine import cycle (``memory → consciousness → prompts.reflection →
# self_calibration``).

#: The closed citation vocabulary — one kind per evidence family a capability
#: claim may anchor to. ``TASK``: a task id that resolved a named outcome; ``SHA``:
#: a merge commit SHA in the version archive; ``SENSOR_TAG``: a behavioural-sensor
#: tag the loop emits. Kept a ``Literal`` so a new family is a one-line edit.
CitationKind = Literal["TASK", "SHA", "SENSOR_TAG"]

#: The closed resolution vocabulary — the whole range
#: :func:`resolve_claim_citation` (via the parse+resolve pair) can report.
#: ``resolved``: the anchor exists and points the right way (a successful task, a
#: matching archived SHA, an emitted tag). ``absent``: the anchor names nothing the
#: caller can see (an unknown task id, no matching SHA, a tag no sensor emits).
#: ``wrong_polarity``: the anchor exists but points the wrong way — a task that did
#: not succeed. ``unresolvable``: the citation text could not be parsed into a
#: :class:`ClaimCitation` at all. Existence-plus-polarity, never entailment.
ClaimResolution = Literal["resolved", "absent", "unresolvable", "wrong_polarity"]


@dataclass(frozen=True)
class ClaimCitation:
    """A parsed, typed evidence citation for one capability claim.

    ``kind`` selects the family; ``target`` is the named anchor (a task id, a
    commit-SHA prefix, or a sensor tag), normalised at parse time — task/SHA
    lowercased (both are hex), sensor tags upper-cased (the lexicon's canonical
    case). Frozen so it is a stable provenance record on the claim's evidence row.
    """

    kind: CitationKind
    target: str


@dataclass(frozen=True)
class ClaimEvidenceSources:
    """The caller-supplied observation bundle a citation is resolved against.

    Every field has an empty default so a caller supplies only what it can see (an
    all-empty bundle resolves every citation to ``absent``), mirroring how
    :class:`CalibrationEvidence` takes every observation from its caller.
    ``successful_task_ids`` / ``failed_task_ids`` are the success and non-success
    slices of ``known_task_ids`` (the set of every task id the caller read):
    membership decides a ``TASK`` citation's polarity. ``archived_shas`` are the
    ``VersionRecord.commit_sha`` values a ``SHA`` citation is prefix-matched
    against. ``sensor_tags`` is the canonical behavioural-sensor tag set a
    ``SENSOR_TAG`` citation must name.
    """

    successful_task_ids: frozenset[str] = frozenset()
    failed_task_ids: frozenset[str] = frozenset()
    known_task_ids: frozenset[str] = frozenset()
    archived_shas: tuple[str, ...] = ()
    sensor_tags: frozenset[str] = frozenset()


# One anchored, keyword-prefixed regex per citation family, case-insensitive. The
# leading keyword disambiguates the kind (``task`` vs ``sha`` share a hex body), so
# at most one matches. Anchored ``^…$`` with an optional trailing period, mirroring
# the loss-condition family regexes above. Task ids are ``uuid.uuid4().hex[:12]``
# (12 hex); a SHA may be abbreviated, so both accept a 6-40 hex run.
_CLAIM_TASK_RE = re.compile(r"^task\s+(?P<target>[0-9a-f]{6,40})\.?$", re.IGNORECASE)
_CLAIM_SHA_RE = re.compile(r"^sha\s+(?P<target>[0-9a-f]{6,40})\.?$", re.IGNORECASE)
_CLAIM_SENSOR_RE = re.compile(
    r"^sensor\s+(?P<target>[A-Za-z0-9][A-Za-z0-9_/-]*)\.?$", re.IGNORECASE
)

#: One canonical citation line per :data:`CitationKind`, in :data:`CitationKind`
#: order — the vocabulary the ``update_self_model`` prompt teaches from behind
#: ``[audit] evidence_bound_claims_enabled``. Declared beside the family regexes so
#: this module stays the single owner of its own citation grammar, and
#: ``tests/test_self_calibration.py`` pins each entry to the :class:`ClaimCitation`
#: its family produces (and the tuple to ``get_args(CitationKind)``) so an example
#: the parser rejects reds the build.
CLAIM_CITATION_GRAMMAR: tuple[str, ...] = (
    "task 3f9a1c2d4e5b",
    "sha 1a2b3c4d",
    "sensor FORCED-QUIET-RATIO",
)


def parse_claim_citation(text: str) -> ClaimCitation | None:
    """Parse a claim-evidence citation string into a typed :class:`ClaimCitation`.

    Each family is tried in turn; because every regex is anchored and
    keyword-prefixed, at most one matches. Returns ``None`` — never raises — on any
    unmatched keyword, malformed body, or empty input, so an unparseable citation
    is recorded ``unresolvable`` rather than crashing the update. Task and SHA
    targets are lowercased (both are hex); sensor tags are upper-cased to the
    lexicon's canonical case.
    """
    stripped = text.strip()
    if not stripped:
        return None
    m = _CLAIM_TASK_RE.match(stripped)
    if m is not None:
        return ClaimCitation(kind="TASK", target=m.group("target").lower())
    m = _CLAIM_SHA_RE.match(stripped)
    if m is not None:
        return ClaimCitation(kind="SHA", target=m.group("target").lower())
    m = _CLAIM_SENSOR_RE.match(stripped)
    if m is not None:
        return ClaimCitation(kind="SENSOR_TAG", target=m.group("target").upper())
    return None


def resolve_claim_citation(
    citation: ClaimCitation, sources: ClaimEvidenceSources
) -> ClaimResolution:
    """Resolve a parsed *citation* against injected *sources* — existence + polarity.

    Never entailment: it only decides whether the anchor exists and points the
    right way. Per kind:

    * ``TASK`` — a target in ``successful_task_ids`` is ``resolved``; one in
      ``failed_task_ids`` is ``wrong_polarity``; one absent from ``known_task_ids``
      is ``absent``; a target known but neither successful nor failed (a
      non-success outcome such as ``partial``) is ``wrong_polarity`` — a real
      anchor, but not the positive outcome a capability claim needs.
    * ``SHA`` — matched case-insensitively as a *prefix* of any archived
      ``commit_sha`` (so an abbreviated citation resolves); ``absent`` otherwise.
    * ``SENSOR_TAG`` — ``resolved`` when the tag is in ``sensor_tags``, else
      ``absent``.

    Pure, deterministic, no I/O. Never returns ``unresolvable`` — that status is
    reserved for a citation :func:`parse_claim_citation` could not parse at all.
    """
    if citation.kind == "TASK":
        if citation.target in sources.successful_task_ids:
            return "resolved"
        if citation.target in sources.failed_task_ids:
            return "wrong_polarity"
        if citation.target not in sources.known_task_ids:
            return "absent"
        return "wrong_polarity"
    if citation.kind == "SHA":
        prefix = citation.target.lower()
        for sha in sources.archived_shas:
            if sha.lower().startswith(prefix):
                return "resolved"
        return "absent"
    # SENSOR_TAG is the only remaining kind.
    return "resolved" if citation.target in sources.sensor_tags else "absent"


def resolve_citation_status(
    text: str, sources: ClaimEvidenceSources
) -> tuple[ClaimCitation | None, ClaimResolution]:
    """Parse then resolve *text*, returning the (citation, status) pair.

    The one-call composition the caller uses: an unparseable *text* returns
    ``(None, "unresolvable")`` (the parse-failure sentinel), otherwise the parsed
    :class:`ClaimCitation` and its :func:`resolve_claim_citation` verdict. Pure and
    I/O-free — every observation still arrives via *sources*.
    """
    citation = parse_claim_citation(text)
    if citation is None:
        return None, "unresolvable"
    return citation, resolve_claim_citation(citation, sources)


@dataclass(frozen=True)
class ConsciousnessCriterion:
    """One functional (engineerable) property of consciousness the report reads.

    Purely declarative — it *names and labels* a property and records whether
    Maestro has built the machinery for it; it references no measure function, so
    this module keeps importing only :mod:`maestro.types` and stays free of the
    ``audit_checks`` / ``consciousness`` import cycle. The consumer
    (``maestro.cli.cmd_consciousness_report``) is the sole reader that binds each
    ``key`` to its measured reading.

    *key* — the stable identifier the ``--json`` payload and tests key off.
    *label* — the human row title.
    *measures* — a one-line scope note: exactly what the row reads and, where it
    matters, what it deliberately does *not* (the grounding row reports existence
    and polarity of citations, never entailment).
    *implemented* — ``True`` when the machinery exists and the report renders a
    measured reading; ``False`` for a deferred global-workspace indicator, which
    renders ``not_implemented`` rather than letting another row stand in for a
    workspace that does not exist yet.
    """

    key: str
    label: str
    measures: str
    implemented: bool


#: The consciousness-report property list — its single home (proposal 129). One
#: entry per *engineerable* functional property, in report order: identity
#: availability, the six global-workspace indicators (limited-capacity
#: selection, receipted broadcast, recurrence, attention-schema agreement,
#: temporal continuity, and causal reach, all grounded in the episode ledger via
#: ``maestro.consciousness_eval`` — proposal 130), perceptual grounding,
#: metacognitive calibration, and narration→actuation conversion. Declarative
#: only — no measure function is referenced here, so the module stays cycle-free;
#: ``cmd_consciousness_report`` assembles one reading per entry.
#: ``tests/test_self_calibration.py`` pins the membership (every entry now
#: ``implemented``), key uniqueness, and well-formedness.
CONSCIOUSNESS_CRITERIA: tuple[ConsciousnessCriterion, ...] = (
    ConsciousnessCriterion(
        key="identity_availability",
        label="Identity availability",
        measures="a durable self-model reaching every model call",
        implemented=True,
    ),
    ConsciousnessCriterion(
        key="limited_capacity_selection",
        label="Limited-capacity moment selection",
        measures=(
            "selection-trace competition within the moment's capacity caps, "
            "folded from the episode ledger — admitted-vs-excluded, never prose"
        ),
        implemented=True,
    ),
    ConsciousnessCriterion(
        key="receipted_broadcast",
        label="Receipted broadcast",
        measures=(
            "delivery-receipt coverage of the authorized consumer registry with "
            "a consistent moment hash, folded from the episode ledger"
        ),
        implemented=True,
    ),
    ConsciousnessCriterion(
        key="recurrence",
        label="Recurrence",
        measures=(
            "verified task outcomes re-entering a later moment as lineage-bearing "
            "successor inputs, folded from the episode ledger — mechanical "
            "provenance only, never prose"
        ),
        implemented=True,
    ),
    ConsciousnessCriterion(
        key="attention_schema_agreement",
        label="Attention-schema agreement",
        measures=(
            "the attention schema's self-reported load and omission agreeing with "
            "the actual selection trace, folded from the episode ledger — counts, "
            "never prose"
        ),
        implemented=True,
    ),
    ConsciousnessCriterion(
        key="temporal_continuity",
        label="Temporal continuity",
        measures=(
            "each episode honestly naming a predecessor or a typed gap across "
            "restarts, folded from the episode ledger — continuity markers, never "
            "prose"
        ),
        implemented=True,
    ),
    ConsciousnessCriterion(
        key="causal_reach",
        label="Causal reach",
        measures=(
            "validated decision-citation uptake — the tasks and plans a "
            "broadcast moment was cited by, folded from the episode ledger "
            "against the citing consumer's delivery receipt; declared uptake, "
            "deliberately not counterfactual causality"
        ),
        implemented=True,
    ),
    ConsciousnessCriterion(
        key="perceptual_grounding",
        label="Perceptual grounding",
        measures=(
            "capability claims bound to resolvable evidence — existence and "
            "polarity of citations, never entailment"
        ),
        implemented=True,
    ),
    ConsciousnessCriterion(
        key="metacognitive_calibration",
        label="Metacognitive calibration",
        measures=(
            "scored self-prediction: accuracy, self-vs-measured agreement, a "
            "Brier score, and the narrated/pending/unresolved shares"
        ),
        implemented=True,
    ),
    ConsciousnessCriterion(
        key="narration_actuation_conversion",
        label="Narration-to-actuation conversion",
        measures=(
            "detected narration problems that reached an actuator and settled "
            "(settlement read from local ledgers only, no live merge check)"
        ),
        implemented=True,
    ),
)
