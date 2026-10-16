"""Gate environment — the pure owner of whether the local gate can reach a verdict.

A local quality-gate run can fail for two very different reasons. The diff can be
wrong — ruff, mypy or pytest names something the change broke. Or the *environment*
can be unable to answer at all: a first-party sibling package that resolves as a
half-empty PEP 420 namespace so ``pytest`` aborts at collection, a suite that cannot
finish inside the deadline it is granted, a runner that never starts. The second
class is not a red any diff can clear, and nothing in the tree could tell the two
apart *before* the expensive work was spent: each site re-derived it afterwards from
gate output text, in its own vocabulary — a ``TIMEOUT`` non-answer in
:mod:`maestro.gate_observation`, an ``UNATTRIBUTABLE`` convergence terminal, a
mypy-only provisioning classifier in :mod:`maestro.red_root`, a second-attempt
disposition rule in :mod:`maestro.attempt_disposition`.

This module is the missing owner, in the shape the sibling gate facts already take:
:mod:`maestro.gate_capacity` owns how wide a run may be and how long it may take,
:mod:`maestro.gate_scope` owns which suite it executes, :mod:`maestro.gate_observation`
owns what its output means, and this one owns *whether it can answer at all*. It is
a zero-SDK, zero-subprocess leaf: every probe takes its evidence injected — a
``find_spec`` callable, a collector callable, a sequence of telemetry readings — so
the module itself spawns nothing and reads only its own cached row.

Its consumers all sit behind the default-off ``[loop]
gate_environment_oracle_enabled``. The approved-plan selector
(``MaestroAgent._implement_approved_plans``) refreshes the verdict once per
iteration before it spends an implementation attempt, and on a non-``SOUND``
verdict holds every plan it would have selected against that verdict's
fingerprint rather than buying a red the diff cannot clear; the convergence
terminal reads the cached row to attribute an ``ENVIRONMENT`` red; and the gate
execution lease (:func:`~maestro.gate_capacity.lease_for_gate_run`) consults it at
admission, so a run whose verdict :func:`declines_gate_run` accepts is refused
before ruff, mypy or pytest is spawned at all. What lands here is pure, measurable
and reversible by deletion: the probes, their precedence, a kind-scoped
fingerprint, a corruption-tolerant cache, the declining predicates, and the
ask-once notice sidecar that keys the outbound question on the *fact* rather than
on its fingerprint.

The one-way import edge is load-bearing: this module imports
:mod:`maestro.gate_observation` (the parser it reuses verbatim) but **never**
:mod:`maestro.gate_capacity`, so a future seam binding the lease to this oracle
cannot recurse back into the collector. The telemetry row the fit probe reads is
matched structurally via :class:`GateRunReading`, not imported.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Callable, Sequence
from importlib.machinery import ModuleSpec
from importlib.util import find_spec
from pathlib import Path
from statistics import median
from typing import Literal, Protocol, TypeGuard

from maestro.fileutil import atomic_write, file_lock
from maestro.gate_observation import (
    GateNonAnswer,
    parse_gate_output,
    parse_non_answer,
)

# ---------------------------------------------------------------------------
# The closed vocabulary
# ---------------------------------------------------------------------------

#: Whether the local gate can reach a verdict, and if not, which way it cannot.
#: ``SOUND`` — every probe passed: a red from this environment is the diff's to
#: answer for. ``UNPROVISIONED`` — a first-party sibling package is missing or
#: resolves as a namespace shadow, so an import will abort before any test runs.
#: ``UNCOLLECTABLE`` — pytest cannot even collect the suite (an import error, a
#: fixture abort). ``UNFIT`` — the suite collects but measurably cannot finish
#: inside the deadline it is granted at the planned width. Kept a ``Literal`` so a
#: further kind is a one-line vocabulary edit.
GateRunnabilityKind = Literal["SOUND", "UNPROVISIONED", "UNCOLLECTABLE", "UNFIT"]

#: One declared submodule per first-party sibling package. The submodule is what
#: makes the check meaningful: ``import maestro_sdk`` *succeeds* under a PEP 420
#: namespace shadow (an empty directory on ``sys.path`` is a valid namespace
#: package), so only resolving a module that must live inside the real
#: distribution exposes the shadow.
_SIBLINGS: tuple[tuple[str, str], ...] = (
    ("maestro_sdk", "maestro_sdk.mcp_config"),
    ("maestro_review", "maestro_review.review"),
)

#: Fraction of a granted deadline the measured p50 may reach before the suite is
#: read as unfit. Below 1.0 because a p50 *at* the deadline means half the runs
#: are killed; the margin is what keeps a marginal suite from oscillating.
_FIT_P50_FRACTION = 0.9

#: How many recent qualifying rows the censored-timeout reading looks at, and how
#: many of them must have been killed for the environment to read as unfit. A
#: killed pytest can never report a figure above its own deadline, so a majority
#: of timeouts is the *only* evidence a right-censored sample can offer.
_FIT_TIMEOUT_WINDOW = 5
_FIT_TIMEOUT_THRESHOLD = 3

#: How many recent completed rows the p50 reading folds. Three is the smallest
#: sample a median can be taken of without being a single reading.
_FIT_COMPLETED_WINDOW = 3

#: The outcomes whose ``tests_seconds`` measures *the suite* rather than where
#: the run was cut off. ``CLEAN`` ran every test; ``COVERAGE`` is a shortfall
#: only computable after the last one did. Every other outcome is truncated or
#: right-censored — a ``-x`` probe stops at its first failure (``FAILED``),
#: collection never reached the tests (``COLLECTION_ERROR``), the runner died
#: (``UNAVAILABLE``), the deadline killed it (``TIMEOUT``, which is stamped in a
#: ``finally`` and so carries roughly the deadline itself), and ``UNKNOWN``
#: holds no claim at all. Folding a censored row into the p50 would let two
#: killed runs — below the 3-of-5 timeout threshold — manufacture a false
#: ``UNFIT``, and a short ``FAILED`` read would mask a real one. Spelled here
#: rather than imported, because the owner guard keeps the edge to
#: :mod:`maestro.gate_capacity` one-way.
_FIT_COMPLETED_OUTCOMES: frozenset[str] = frozenset({"CLEAN", "COVERAGE"})

#: The kinds for which :func:`declines_gate_run` refuses a gate run outright.
#: Named as a set rather than inline at the predicate so the two membership facts
#: — which kinds decline, and which are admitted — live in one place. Deliberately
#: not the whole vocabulary: a kind absent here is admitted by construction.
_DECLINING_KINDS: frozenset[str] = frozenset({"UNPROVISIONED", "UNCOLLECTABLE"})

#: Character ceiling on the single line :func:`format_verdict` renders. A
#: verdict's ``identities`` set is unbounded, so the one context-bloat cap this
#: pure module needs is the one on its own rendering.
_VERDICT_LINE_MAX = 300

#: The gate identity a declined run is reported under when the verdict carries
#: none of its own — public because the synthesizing consumer also falls back to
#: it when a verdict's identities are all outside the ``tests:`` namespace — the collection abort an unimportable sibling actually
#: produces, in the ``GateObservation.signature`` spelling.
DECLINED_IDENTITY = "tests:COLLECTION_ERROR"


@dataclasses.dataclass(frozen=True)
class GateRunnabilityVerdict:
    """One closed answer to "can the local gate reach a verdict?".

    :attr:`kind` is the verdict; :attr:`fingerprint` is **kind-scoped** — it hashes
    only the inputs *this* kind is about, so it moves when the fact moves and not
    otherwise, which is what lets a consumer hold a verdict across iterations
    without re-probing. :attr:`identities` carries gate identities in the
    :meth:`~maestro.gate_observation.GateObservation.signature` spelling
    (``"tests:COLLECTION_ERROR"``), so a consumer can compare a verdict against a
    gate observation with no translation layer; it is empty for every kind but
    ``UNCOLLECTABLE``. :attr:`detail` is the bounded human-readable "why", and
    :attr:`measured_at` the ISO-8601 UTC stamp the caller supplies (this module
    reads no clock). Frozen — a measurement is history.
    """

    kind: GateRunnabilityKind
    fingerprint: str
    identities: frozenset[str] = frozenset()
    detail: str = ""
    measured_at: str = ""


class GateRunReading(Protocol):
    """The telemetry fields :func:`probe_fit` reads, matched structurally.

    ``maestro.gate_capacity.GateRunRecord`` satisfies this by construction, so a
    caller passes its records straight in — without this module importing the
    lease, which is the one-way edge the owner guard pins.
    """

    @property
    def outcome(self) -> str: ...
    @property
    def workers(self) -> int: ...
    @property
    def source(self) -> str: ...
    @property
    def scope(self) -> str: ...
    @property
    def tests_seconds(self) -> float: ...


def _fingerprint(kind: GateRunnabilityKind, *parts: str) -> str:
    """Hash *kind* plus its own scoped inputs into a short stable digest.

    The kind leads so two kinds can never collide on identical inputs. Pure.
    """
    payload = "\x1f".join((kind, *parts))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _resolves(
    name: str, finder: Callable[[str], ModuleSpec | None],
) -> bool:
    """Whether *name* resolves to a real, non-namespace module via *finder*.

    A :class:`~importlib.machinery.ModuleSpec` whose ``origin`` is ``None`` is the
    namespace portion — a directory with no ``__init__.py`` — and reads as
    unresolved. A ``find_spec`` that raises ``ModuleNotFoundError`` (the parent
    package itself is absent) or ``ValueError`` (a shadowed parent left a
    ``__spec__``-less module in ``sys.modules``) reads as unresolved too rather
    than propagating: those are exactly the shapes the probe exists to report.
    """
    try:
        spec = finder(name)
    except (ModuleNotFoundError, ValueError):
        return False
    return spec is not None and spec.origin is not None


def _repair(package: str) -> str:
    """The editable-install line that repairs an unprovisioned *package*.

    One spelling of the remediation, so both ``UNPROVISIONED`` branches — and
    therefore every consumer that surfaces :attr:`GateRunnabilityVerdict.detail`
    — name the same command. Pure.
    """
    return f'install the sibling: pip install -e "{package}/"'


def probe_provisioning(
    *,
    finder: Callable[[str], ModuleSpec | None] | None = None,
    measured_at: str = "",
) -> GateRunnabilityVerdict:
    """Whether every first-party sibling package is really installed.

    Returns ``UNPROVISIONED`` naming the first :data:`_SIBLINGS` pair whose package
    or declared submodule does not resolve, and ``SOUND`` when all of them do. The
    fingerprint hashes the unresolved names, so a second sibling breaking moves it.
    Pure: *finder* is injected, no subprocess, no disk.

    Both ``UNPROVISIONED`` details close with the concrete repair — the editable
    install of the package that broke. The detail is the string every consumer
    surfaces (the repair directive, the ask-once message, the recorded blocker,
    the ``maestro preflight`` FAIL line), so spelling the remediation at the one
    place that knows *which* sibling is missing makes all of them actionable
    without any of them re-deriving it.

    The default *finder* is resolved from this module's own :func:`find_spec`
    binding at *call* time rather than captured in the signature, so the one
    impurity the probe has left stays substitutable — a caller that cannot
    inject (``maestro preflight``, which wants the real interpreter's answer)
    still leaves it testable.
    """
    resolve = find_spec if finder is None else finder
    for package, submodule in _SIBLINGS:
        if not _resolves(package, resolve):
            return GateRunnabilityVerdict(
                kind="UNPROVISIONED",
                fingerprint=_fingerprint("UNPROVISIONED", package),
                detail=(
                    f"{package} does not resolve — {_repair(package)}"
                ),
                measured_at=measured_at,
            )
        if not _resolves(submodule, resolve):
            return GateRunnabilityVerdict(
                kind="UNPROVISIONED",
                fingerprint=_fingerprint("UNPROVISIONED", package, submodule),
                detail=(
                    f"{package} resolves but {submodule} does not — "
                    f"namespace shadow — {_repair(package)}"
                ),
                measured_at=measured_at,
            )
    return GateRunnabilityVerdict(
        kind="SOUND",
        fingerprint=_fingerprint("SOUND"),
        detail="siblings provisioned",
        measured_at=measured_at,
    )


def probe_collection(
    collector: Callable[[], tuple[int, str, str] | GateNonAnswer],
    *,
    head: str = "",
    measured_at: str = "",
) -> GateRunnabilityVerdict:
    """Whether pytest can collect the suite at all.

    *collector* returns either a finished ``pytest --collect-only -q
    -p no:cacheprovider`` run as ``(returncode, stdout, stderr)`` — parsed through
    :func:`~maestro.gate_observation.parse_gate_output` — or a bare ``"TIMEOUT"`` /
    ``"UNAVAILABLE"`` literal, its own non-answer, parsed through
    :func:`~maestro.gate_observation.parse_non_answer`. Either way the resulting
    observation decides: ``CLEAN`` is ``SOUND``; anything else is
    ``UNCOLLECTABLE`` carrying that observation's
    :meth:`~maestro.gate_observation.GateObservation.signature` as
    :attr:`~GateRunnabilityVerdict.identities`. There is no rival parser here and
    this module runs no subprocess — the collector is the caller's.

    *head* is the ``origin/main`` head the reading was taken at; it enters the
    ``UNCOLLECTABLE`` fingerprint because a merge can repair a collection red in
    code, so the same failure at a new head is a new fact worth re-probing.
    """
    result = collector()
    if isinstance(result, str):
        observation = parse_non_answer("tests", result, "", "")
    else:
        returncode, stdout, stderr = result
        observation = parse_gate_output("tests", stdout, stderr, returncode)
    if observation.kind == "CLEAN":
        return GateRunnabilityVerdict(
            kind="SOUND",
            fingerprint=_fingerprint("SOUND"),
            detail="suite collects",
            measured_at=measured_at,
        )
    identities = observation.signature()
    return GateRunnabilityVerdict(
        kind="UNCOLLECTABLE",
        fingerprint=_fingerprint(
            "UNCOLLECTABLE", head, *sorted(identities),
        ),
        identities=identities,
        detail=f"collection {observation.kind}",
        measured_at=measured_at,
    )


def probe_fit(
    readings: Sequence[GateRunReading],
    *,
    workers: int,
    deadline_seconds: int,
    cap_seconds: int = 0,
    measured_at: str = "",
) -> GateRunnabilityVerdict:
    """Whether the suite measurably finishes inside the deadline it is granted.

    Reads only the rows that measured *this* question: ``source == "in_process"``
    (the only runs a deadline was enforced on) and ``scope == "full"`` (a narrowed
    run's cost is not the suite's) at the given *workers* width. Two independent
    readings return ``UNFIT``:

    * at least :data:`_FIT_TIMEOUT_THRESHOLD` of the last
      :data:`_FIT_TIMEOUT_WINDOW` qualifying rows ended ``TIMEOUT`` — the
      right-censored reading, since a killed pytest can never report a figure
      above its own deadline; or
    * the last :data:`_FIT_COMPLETED_WINDOW` rows that actually ran to
      completion (an outcome in :data:`_FIT_COMPLETED_OUTCOMES` carrying a
      *positive* ``tests_seconds``) have a median above
      ``_FIT_P50_FRACTION * deadline``.

    Both readings are judged against *cap_seconds* when it is positive, because
    ``derive_deadline`` *escalates*: a suite killed at its budget buys a bigger
    one on the next run, floored at ``[timeouts] selfcheck`` and capped at
    ``[timeouts] selfcheck_max``. The intermediate rungs of that climb are
    precisely the runs whose job is to discover how long the suite takes, so
    calling them a broken environment would park every approved plan on a hold
    while the escalation that would have rescued them was still in progress. A
    positive cap therefore means: the censored arm returns ``UNFIT`` only once
    the granted deadline has reached the cap — the escalation has nothing left
    to buy — and the p50 arm compares the median against
    ``_FIT_P50_FRACTION * cap_seconds`` rather than the current rung. A reading
    spared only because the escalation is still climbing returns ``SOUND`` with
    ``detail="escalating"`` — legible as a suite under real pressure rather than
    silently indistinguishable from one that comfortably fits.
    ``cap_seconds=0`` is "no cap known" and is byte-identical to the pre-cap
    behaviour.

    No qualifying rows is ``SOUND`` with ``detail="unmeasured"`` — the honest
    "nothing measured yet", never a synthesised pass on evidence. *workers*,
    *deadline_seconds* and *cap_seconds* are the caller's, supplied from
    ``uncontended_width`` / ``derive_deadline`` / ``[timeouts] selfcheck_max``; nothing
    is re-derived here — that is the one-way import edge to
    :mod:`maestro.gate_capacity` this module exists on the leaf side of. Pure,
    zero I/O.
    """
    qualifying = [
        r
        for r in readings
        if r.source == "in_process" and r.scope == "full" and r.workers == workers
    ]
    if not qualifying:
        return GateRunnabilityVerdict(
            kind="SOUND",
            fingerprint=_fingerprint("SOUND"),
            detail="unmeasured",
            measured_at=measured_at,
        )
    unfit = _fingerprint(
        "UNFIT", str(workers), str(deadline_seconds), str(cap_seconds),
    )
    # Whether the escalation still has something to buy, and the ceiling the
    # readings are judged against — the cap when one is known, else the rung that
    # was granted. Named once so the two arms can never read different ceilings.
    climbing = cap_seconds > 0 and deadline_seconds < cap_seconds
    ceiling = cap_seconds if cap_seconds > 0 else deadline_seconds
    # What the ceiling *is*, so the detail cannot report a cap as if it were the
    # budget the run was actually killed at.
    ceiling_name = "cap" if cap_seconds > 0 else "deadline"
    # Whether a reading that *would* have been ``UNFIT`` at the granted rung was
    # spared only because the escalation has more to buy. Set by whichever arm
    # spares it, so ``detail="escalating"`` names a suite under real pressure
    # rather than every healthy suite whose rung sits below the cap.
    escalating = False
    recent = qualifying[-_FIT_TIMEOUT_WINDOW:]
    timeouts = sum(1 for r in recent if r.outcome == "TIMEOUT")
    if timeouts >= _FIT_TIMEOUT_THRESHOLD and climbing:
        escalating = True
    elif timeouts >= _FIT_TIMEOUT_THRESHOLD:
        return GateRunnabilityVerdict(
            kind="UNFIT",
            fingerprint=unfit,
            detail=(
                f"{timeouts} of the last {len(recent)} runs at {workers} "
                "worker(s) were killed at their deadline"
            ),
            measured_at=measured_at,
        )
    completed = [
        r.tests_seconds
        for r in qualifying
        if r.outcome in _FIT_COMPLETED_OUTCOMES and r.tests_seconds > 0
    ]
    if completed:
        p50 = median(completed[-_FIT_COMPLETED_WINDOW:])
        if p50 > _FIT_P50_FRACTION * deadline_seconds and climbing:
            escalating = True
        if p50 > _FIT_P50_FRACTION * ceiling:
            return GateRunnabilityVerdict(
                kind="UNFIT",
                fingerprint=unfit,
                detail=(
                    f"p50 {p50:.0f}s exceeds {_FIT_P50_FRACTION:g} of the "
                    f"{ceiling}s {ceiling_name} at {workers} worker(s)"
                ),
                measured_at=measured_at,
            )
    return GateRunnabilityVerdict(
        kind="SOUND",
        fingerprint=_fingerprint("SOUND"),
        detail="escalating" if escalating else "suite fits its deadline",
        measured_at=measured_at,
    )


def probe_runnability(
    *,
    collector: Callable[[], tuple[int, str, str] | GateNonAnswer],
    readings: Sequence[GateRunReading],
    workers: int,
    deadline_seconds: int,
    cap_seconds: int = 0,
    head: str = "",
    finder: Callable[[str], ModuleSpec | None] | None = None,
    measured_at: str = "",
) -> GateRunnabilityVerdict:
    """Run the three probes in precedence and return exactly one verdict.

    ``UNPROVISIONED`` -> ``UNCOLLECTABLE`` -> ``UNFIT``, cheapest and most
    fundamental first: a missing sibling is why collection would fail, and a suite
    that cannot be collected has no measurable fit. The collector is only invoked
    once provisioning is ``SOUND``, so a broken environment never pays for a
    collection run. ``SOUND`` when all three pass.

    *cap_seconds* is forwarded to :func:`probe_fit` unchanged and never derived
    here: the escalation cap is the caller's evidence, like *workers* and
    *deadline_seconds*, which is what keeps this module a leaf of
    :mod:`maestro.gate_capacity`.
    """
    provisioning = probe_provisioning(finder=finder, measured_at=measured_at)
    if provisioning.kind != "SOUND":
        return provisioning
    collection = probe_collection(collector, head=head, measured_at=measured_at)
    if collection.kind != "SOUND":
        return collection
    return probe_fit(
        readings,
        workers=workers,
        deadline_seconds=deadline_seconds,
        cap_seconds=cap_seconds,
        measured_at=measured_at,
    )


def _verdict_payload(
    verdict: GateRunnabilityVerdict, iteration_key: str,
) -> dict[str, object]:
    """Render *verdict* plus its iteration key as the JSON row shape."""
    return {
        "kind": verdict.kind,
        "fingerprint": verdict.fingerprint,
        "identities": sorted(verdict.identities),
        "detail": verdict.detail,
        "measured_at": verdict.measured_at,
        "iteration_key": iteration_key,
    }


def _parse_row(data: object) -> tuple[GateRunnabilityVerdict, str] | None:
    """Coerce a decoded row into ``(verdict, iteration_key)``, or ``None``.

    Corruption-tolerant by construction: a row that is not a dict, carries a kind
    outside the closed vocabulary, or types a field wrongly returns ``None`` — the
    honest "nothing probed yet" — rather than a synthesised ``SOUND``.
    """
    if not isinstance(data, dict):
        return None
    kind = data.get("kind")
    if kind not in ("SOUND", "UNPROVISIONED", "UNCOLLECTABLE", "UNFIT"):
        return None
    fingerprint = data.get("fingerprint")
    identities = data.get("identities")
    detail = data.get("detail")
    measured_at = data.get("measured_at")
    iteration_key = data.get("iteration_key")
    if not isinstance(fingerprint, str) or not isinstance(iteration_key, str):
        return None
    if not isinstance(identities, list) or not all(
        isinstance(i, str) for i in identities
    ):
        return None
    if not isinstance(detail, str) or not isinstance(measured_at, str):
        return None
    verdict = GateRunnabilityVerdict(
        kind=kind,
        fingerprint=fingerprint,
        identities=frozenset(identities),
        detail=detail,
        measured_at=measured_at,
    )
    return verdict, iteration_key


def read_verdict(path: Path) -> GateRunnabilityVerdict | None:
    """Read the cached verdict row at *path* — the zero-subprocess reader.

    Returns ``None`` for a missing file, an unreadable one, or malformed JSON
    (``OSError`` covers both file shapes, ``ValueError`` subsumes
    ``json.JSONDecodeError``):
    the honest "nothing probed yet". It never crashes and never synthesises a
    ``SOUND``, because a consumer that reads soundness out of an absent
    measurement is exactly the failure this owner exists to prevent.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    parsed = _parse_row(data)
    return parsed[0] if parsed is not None else None


def format_verdict(verdict: GateRunnabilityVerdict) -> str:
    """Render *verdict* as one bounded operator-facing line.

    The renderer lives in the owner rather than at each command that prints a
    verdict, so no CLI module spells a :data:`GateRunnabilityKind` member as a
    literal — which is exactly the property the owner guard pins. The line leads
    with the kind, carries the :attr:`~GateRunnabilityVerdict.detail` (already the
    actionable "why", closing with the repair on an ``UNPROVISIONED``) and, only
    when non-empty, the sorted :attr:`~GateRunnabilityVerdict.identities` the
    environment cannot answer for.

    Bounded at :data:`_VERDICT_LINE_MAX` characters because a verdict's identities
    are an unbounded set — a suite whose every module fails to import would
    otherwise render a screenful where one line was promised. Truncation appends
    an ellipsis so a clipped line reads as clipped. Pure, no I/O.
    """
    line = f"gate runnability: {verdict.kind}"
    if verdict.detail:
        line += f" — {verdict.detail}"
    if verdict.identities:
        line += f" [{', '.join(sorted(verdict.identities))}]"
    if len(line) > _VERDICT_LINE_MAX:
        return line[: _VERDICT_LINE_MAX - 1] + "…"
    return line


def declines_gate_run(verdict: GateRunnabilityVerdict) -> bool:
    """Whether *verdict* means a gate run would spend its subprocesses for nothing.

    The declining half of the vocabulary is the half where the environment cannot
    answer *at all*: a sibling distribution that will abort the first import, or a
    suite that cannot be collected. Neither is a red a diff can clear, and neither
    needs ruff, mypy or pytest to be spawned to be known — the cached verdict
    already holds it.

    The other two kinds are deliberately admitted. A sound environment is the
    ordinary case, and a suite that merely fits its granted deadline poorly is
    still worth running for what it enumerates: a partial, timed-out reading names
    real failures, so refusing it would trade a slow answer for none.

    Spelled here rather than at the seam so no consumer re-derives which kinds
    decline — the predicate is the whole vocabulary a caller needs.
    """
    return verdict.kind in _DECLINING_KINDS


def declined_gate_identities(verdict: GateRunnabilityVerdict) -> frozenset[str]:
    """The gate identities a run declined on *verdict* should be reported under.

    A verdict that carries :attr:`~GateRunnabilityVerdict.identities` already
    names, in the
    :meth:`~maestro.gate_observation.GateObservation.signature` spelling, exactly
    which gate identities the environment cannot answer for; those pass through
    untouched, so a consumer's synthesized observation compares equal to the
    verdict the convergence terminal will attribute it against.

    A verdict that carries none makes the whole-run claim instead, and the honest
    identity for it is the collection abort an unimportable first-party sibling
    actually produces: the first ``pytest`` command would die at collection, never
    reaching a test. So the synthesized identity is that abort, and the caller
    needs no fallback of its own.
    """
    return verdict.identities or frozenset({DECLINED_IDENTITY})


def blames_missing_sibling(
    verdict: GateRunnabilityVerdict | None,
) -> TypeGuard[GateRunnabilityVerdict]:
    """Whether *verdict* attributes a red lint or type gate to provisioning.

    ``True`` iff a verdict was measured and names a missing or broken
    first-party sibling install — the one environment fact that explains a red
    mypy run without a code defect. ruff resolves no imports, so on a red ruff
    it is context the reader should see, not the cause. ``UNCOLLECTABLE`` and
    ``UNFIT`` are pytest facts and explain neither, which is why this is not
    :func:`declines_gate_run`. ``None`` (nothing probed yet) blames nothing.
    Spelled here so no consumer names a kind literal.
    """
    return verdict is not None and verdict.kind == "UNPROVISIONED"


def refresh_verdict(
    path: Path,
    *,
    iteration_key: str,
    collector: Callable[[], tuple[int, str, str] | GateNonAnswer],
    readings: Sequence[GateRunReading],
    workers: int,
    deadline_seconds: int,
    cap_seconds: int = 0,
    head: str = "",
    finder: Callable[[str], ModuleSpec | None] | None = None,
    measured_at: str = "",
) -> GateRunnabilityVerdict:
    """Probe at most once per *iteration_key* and persist the row at *path*.

    A cached row stamped with the same *iteration_key* is returned as-is and
    **no** probe is issued — the collector is a whole pytest collection run, so
    re-probing it per phase within one iteration would be the cost this oracle
    exists to avoid. Any other state (missing, corrupt, a previous iteration)
    probes afresh and writes the row under
    :func:`~maestro.fileutil.file_lock` + :func:`~maestro.fileutil.atomic_write`,
    the house JSON state-file pair. The parent directory is created first, since
    ``file_lock`` opens its adjacent ``.lock`` file with ``O_CREAT`` and would
    otherwise raise on a checkout whose memory directory does not exist yet.
    UTF-8 explicit on both sides.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        try:
            cached = _parse_row(
                json.loads(path.read_text(encoding="utf-8")),
            )
        except (OSError, ValueError):
            cached = None
        if cached is not None and cached[1] == iteration_key:
            return cached[0]
        verdict = probe_runnability(
            collector=collector,
            readings=readings,
            workers=workers,
            deadline_seconds=deadline_seconds,
            cap_seconds=cap_seconds,
            head=head,
            finder=finder,
            measured_at=measured_at,
        )
        atomic_write(
            path, json.dumps(_verdict_payload(verdict, iteration_key)),
        )
        return verdict


# ---------------------------------------------------------------------------
# The ask-once notice sidecar
# ---------------------------------------------------------------------------


def notice_key(verdict: GateRunnabilityVerdict) -> str:
    """Render the ``(kind, sorted(identities))`` pair of *verdict* as one key.

    This is deliberately *not* the fingerprint. A fingerprint moves whenever any
    input the kind is scoped to moves — an ``UNCOLLECTABLE`` one carries the
    ``origin/main`` head, so it changes on every merge while the very same
    collection error stands. Keying the ask on the fingerprint would therefore
    re-ask on each merge about a fact nobody has repaired. The
    ``(kind, identities)`` pair is the *fact itself*: a genuinely new way the
    environment cannot answer is a new key and earns a new ask, while a moving
    head under a standing failure is not. Pure; opaque by construction — this
    module stores a key, never a message, so it stays messaging-blind.
    """
    return "\x1f".join((verdict.kind, *sorted(verdict.identities)))


def read_notice(path: Path) -> str | None:
    """Read the last-asked notice key at *path*, or ``None``.

    Corruption-tolerant in the same shape as :func:`read_verdict`: a missing
    file, an unreadable one, malformed JSON, a non-dict row, or a non-string
    ``key`` all read as ``None`` — the honest "nothing asked yet", which costs at
    most one redundant question rather than silencing a genuine one.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    key = data.get("key")
    return key if isinstance(key, str) else None


def record_notice(path: Path, key: str) -> None:
    """Persist *key* as the last-asked notice at *path*.

    Single-field JSON row written under :func:`~maestro.fileutil.file_lock` +
    :func:`~maestro.fileutil.atomic_write`, the house pair, with the parent
    directory created first for the same reason :func:`refresh_verdict` does it.
    Writing an empty key is meaningful: a ``SOUND`` verdict clears the notice, so
    a recurrence after a repair is a fresh ask.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        atomic_write(path, json.dumps({"key": key}))
