"""Calibration and prediction measures for the mechanical self-audit.

Split out of :mod:`maestro.audit_checks` by proposal 147 Step 9: the
premise ledger, narration-conversion measure, task-outcome predictions,
reflection bets, calibration evidence, and data-sufficiency context.
Zero SDK cost.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, get_args, runtime_checkable

from maestro.audit_checks import (
    GATE_ACTIVATIONS_FILENAME,
    GateActivationBackend,
    classify_task_shape,
    extract_issue_numbers,
)
from maestro.fileutil import file_lock
from maestro.memory import (
    Memory,
)
from maestro.self_calibration import (
    MAX_SCOREABLE_AGE_CYCLES,
    CalibrationEvidence,
    CalibrationVerdict,
    LossProbe,
    parse_loss_condition,
    stale_repeat_loss_conditions,
)
from maestro.types import TaskOutcome

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Premise-accuracy leading indicator (issue #2304) — a zero-SDK trace that
# measures, at the moment each generated task is accepted into the queue, how
# many of the ``.py`` file paths its goal references actually exist on disk. A
# large fraction of self-directed tasks start from a wrong *structural premise*
# (referencing a renamed / deleted / hallucinated file); every prior instrument
# is a lagging indicator that only confirms wasted iterations after the fact.
# This mirrors the gate-activation trace arc (log to a store, read with a CLI,
# decide later whether a corrective actuator is worth building): it is
# measurement-only and advisory — it injects nothing into any prompt and drops
# no task. Both a JSONL and a SQLite adapter satisfy :class:`PremiseBackend`, so
# the accept-site writer and the CLI reader never branch on which store is live
# (the proposal 106 dual-store fold).
# ---------------------------------------------------------------------------

PREMISE_OBSERVATIONS_FILENAME = "premise_observations.jsonl"

#: Default number of recent premise-miss task ids surfaced in a
#: :class:`PremiseReport` (newest-first).
_PREMISE_RECENT_MISS_LIMIT = 20


@dataclass(frozen=True)
class PremiseObservation:
    """One task-generation premise measurement, recorded at task-accept time.

    *task_id* — the accepted :class:`~maestro.task_queue.QueuedTask`'s id.
    *referenced* — count of distinct ``.py`` paths the goal named.
    *missing* — how many of those paths do not exist on disk.
    *timestamp* — ISO-8601 UTC timestamp.

    A row with ``referenced == 0`` carries no premise to measure (excluded from
    the accuracy denominator); ``referenced >= 1 and missing == 0`` is a
    premise-hit; ``missing >= 1`` is a premise-miss.
    """

    task_id: str
    referenced: int
    missing: int
    timestamp: str


@dataclass(frozen=True)
class PremiseReport:
    """The accumulated premise-accuracy summary over a trace tail.

    Returned by :func:`measure_premise_accuracy` and consumed (printed) by the
    ``measure-premise-accuracy`` CLI — its non-test call site.

    *total_measured* — rows referencing at least one ``.py`` path (the
    denominator; rows with no premise are excluded).
    *hits* — measured rows with zero missing files.
    *accuracy* — ``hits / total_measured`` (``0.0`` when nothing measured).
    *recent_misses* — task ids of the most recent premise-miss rows,
    newest-first, bounded to the report's recent-limit.
    """

    total_measured: int
    hits: int
    accuracy: float
    recent_misses: list[str]


@runtime_checkable
class PremiseBackend(Protocol):
    """The premise-observation trace persistence surface (issue #2304).

    A pure append trace with no keyed-replace or retraction shape — one row per
    accepted task, append-only, oldest-first load — so it can live in
    ``premise_observations.jsonl`` (default) or in ``state.db`` behind
    ``[paths] sqlite_state_enabled`` with no call-site branching, mirroring
    :class:`GateActivationBackend`. ``@runtime_checkable`` enables a structural
    ``isinstance`` check in the backend-selection tests.
    """

    def append(self, record: PremiseObservation) -> None:
        """Append exactly one premise-observation row."""
        ...

    def read_all(self) -> list[PremiseObservation]:
        """Return every recorded premise observation oldest-first."""
        ...


def _parse_premise_observation_dict(
    data: dict[str, object], *, source: Path | None = None,
) -> PremiseObservation | None:
    """Coerce an already-decoded JSON row into a :class:`PremiseObservation`.

    Field-filters against ``{f.name for f in fields(PremiseObservation)}`` (so
    an unknown future field is dropped, not fatal) and returns ``None`` on any
    structural failure (``KeyError`` / ``TypeError`` / ``AttributeError``) — a
    row missing a required field is dropped, not raised. Mirrors
    :func:`_parse_gate_activation_dict`. *source*, when given, names the ledger
    in the warning.
    """
    known = {f.name for f in fields(PremiseObservation)}
    try:
        filtered = {k: v for k, v in data.items() if k in known}
        return PremiseObservation(**filtered)  # type: ignore[arg-type]
    except (KeyError, TypeError, AttributeError):
        logger.warning(
            "Skipping corrupted premise_observation row in %s", source,
        )
        return None


def load_all_premise_observations(data_dir: Path) -> list[PremiseObservation]:
    """Parse every well-formed row from ``premise_observations.jsonl``.

    The shared reader — the SQLite seed source and the JSONL adapter's
    :meth:`~JsonlPremiseLedger.read_all` — carries the corrupted-line tolerance
    the gate-activation reader has: it **skips corrupt rows and never raises** (a
    malformed line anywhere is dropped, not propagated), and returns ``[]`` on a
    missing file or an ``OSError``. Order is oldest-first, so ``read_all()[-n:]``
    is the tail window the CLI takes.
    """
    path = data_dir / PREMISE_OBSERVATIONS_FILENAME
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[PremiseObservation] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        record = _parse_premise_observation_dict(row, source=path)
        if record is not None:
            records.append(record)
    return records


class JsonlPremiseLedger:
    """JSONL-backed :class:`PremiseBackend` — the default, flag-off store.

    Binds a ``data_dir`` and keeps the ledger on ``premise_observations.jsonl``.
    :meth:`append` writes one row under an advisory
    :func:`~maestro.fileutil.file_lock` around an ``open(path, "a")`` append of
    ``json.dumps(asdict(record)) + "\\n"`` (parent-dir-created) so concurrent
    appends never interleave partial lines; :meth:`read_all` reuses the
    corrupt-tolerant forward parse via :func:`load_all_premise_observations`.
    Mirrors :class:`JsonlGateActivationLedger`.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._history_path = data_dir / PREMISE_OBSERVATIONS_FILENAME

    def append(self, record: PremiseObservation) -> None:
        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        with (
            file_lock(self._history_path),
            open(self._history_path, "a", encoding="utf-8") as f,
        ):
            f.write(json.dumps(asdict(record)) + "\n")

    def read_all(self) -> list[PremiseObservation]:
        return load_all_premise_observations(self._data_dir)


def log_premise_observation(
    backend: PremiseBackend,
    task_id: str,
    referenced: int,
    missing: int,
) -> None:
    """Stamp one premise measurement with the UTC timestamp and append it.

    Builds a :class:`PremiseObservation` (the ``datetime.now(tz=UTC)`` stamp is
    authored here) and delegates persistence to *backend* — the flag-selected
    :class:`JsonlPremiseLedger` (default) or
    :class:`~maestro.state_store.SqlitePremiseLedger`. Zero SDK cost; called at
    the task-accept site in :mod:`maestro.loop.task_generation` alongside the
    sibling :func:`log_gate_activation` writes. Mirrors that writer.
    """
    record = PremiseObservation(
        task_id=task_id,
        referenced=referenced,
        missing=missing,
        timestamp=datetime.now(tz=UTC).isoformat(),
    )
    backend.append(record)


def measure_premise_accuracy(
    backend: PremiseBackend,
    n: int = 100,
    recent_limit: int = _PREMISE_RECENT_MISS_LIMIT,
) -> PremiseReport:
    """Summarise premise accuracy from the tail of the observation trace.

    Reads the last *n* rows via ``backend.read_all()`` and, over the rows
    carrying a premise to measure (``referenced >= 1``), computes the measured
    total, the hit count (``missing == 0``), the accuracy (hits / total, ``0.0``
    when nothing measured), and the most recent premise-miss task ids
    (newest-first, bounded to *recent_limit*). Rows with ``referenced == 0``
    have no premise and are excluded from every count. Pure read — no mutations;
    zero SDK cost. Mirrors :func:`measure_gate_triggers`.
    """
    records = backend.read_all()
    tail = records[-n:] if n > 0 else []
    total_measured = 0
    hits = 0
    misses: list[str] = []
    for record in tail:
        if not isinstance(record.referenced, int) or record.referenced < 1:
            continue
        total_measured += 1
        missing = record.missing if isinstance(record.missing, int) else 0
        if missing == 0:
            hits += 1
        elif isinstance(record.task_id, str) and record.task_id:
            misses.append(record.task_id)
    accuracy = hits / total_measured if total_measured > 0 else 0.0
    recent = list(reversed(misses[-recent_limit:])) if recent_limit > 0 else []
    return PremiseReport(
        total_measured=total_measured,
        hits=hits,
        accuracy=accuracy,
        recent_misses=recent,
    )


# ---------------------------------------------------------------------------
# Narration→action conversion sensor (proposal 090)
# ---------------------------------------------------------------------------

# Gate strings the narration→action writers stamp into
# ``gate_activations.jsonl``. Each is a zero-SDK-cost actuator that turns a
# detected narration problem (a landing-claiming ``success``, a phantom
# escalation, an unconfirmed handoff) into a durable mechanical act:
#   * The rule-14 PR-less arm (``rule_14_pr_less``) offers fire-or-close
#     ``next_task_directive``s — logged ``directive_set`` when the offer filled
#     the empty slot, ``dropped`` when the slot was already taken (a drop the
#     replay ledger may re-surface).
#   * The audit→directive bridge (``audit_directive_bridge``) routes the first
#     audit finding of a cycle into an empty slot — ``directive_set``.
#   * The forced arms (``phantom_escalation_delivery``,
#     ``unconfirmed_landing_forced``) deliver a
#     merge-or-close message + durable
#     standdown — ``handoff`` (logged only when the delivery actually happened,
#     never on an idempotent no-op).
#   * ``dropped_directive_replay`` is the chain gate that re-offers a dropped
#     directive — ``directive_set`` (already logged by ``run_gate_chain``).
# :func:`measure_narration_conversion` reads these rows to report how often a
# detection became a durable actuation and how often that actuation's grounding
# later settled.
NARRATION_ACTUATION_GATES: frozenset[str] = frozenset({
    "rule_14_pr_less",
    "audit_directive_bridge",
    "phantom_escalation_delivery",
    "unconfirmed_landing_forced",
    "dropped_directive_replay",
})

# Actions that count as a durable actuation: a directive filled a slot, or a
# handoff was delivered. ``dropped`` is a detection that *lost* the slot race —
# counted on its own, never as an actuation.
_NARRATION_ACTUATION_ACTIONS: frozenset[str] = frozenset({
    "directive_set", "handoff",
})

# Recovers a ``sha=<hex>`` grounding token from an activation detail. The §5c-iv
# and rule-14 PR-less writers stamp ``sha=<hex>``; a bare SHA echoed in free
# directive text is intentionally not matched — only the explicit token is a
# grounding claim the settlement join trusts.
_SHA_DETAIL_RE = re.compile(r"sha=([0-9a-fA-F]{7,40})")


def _narration_grounding_settled(
    detail: str,
    is_pr_merged: Callable[[int], bool],
    is_sha_landed: Callable[[str], bool],
) -> bool:
    """Whether an actuation's parsed grounding shows merged/landed now.

    Parses ``#NNNN`` PR numbers (:func:`extract_issue_numbers`) and
    ``sha=<hex>`` tokens (:data:`_SHA_DETAIL_RE`) out of *detail* and returns
    ``True`` when at least one is shown settled by the injected oracles.
    Returns ``False`` when *detail* carries no parseable grounding — there is
    nothing to confirm, so the row falls out of the settlement numerator.
    """
    prs = extract_issue_numbers(detail)
    shas = set(_SHA_DETAIL_RE.findall(detail))
    if not prs and not shas:
        return False
    if any(is_pr_merged(pr) for pr in prs):
        return True
    return any(is_sha_landed(sha) for sha in shas)


def measure_narration_conversion(
    backend: GateActivationBackend,
    n: int = 100,
    *,
    is_pr_merged: Callable[[int], bool],
    is_sha_landed: Callable[[str], bool],
) -> dict[str, object]:
    """Report the narration→action conversion pipeline from the activation trace.

    Reads the trailing *n* rows via ``backend.read_all()`` (same tail-read and
    corrupted-row tolerance as :func:`measure_gate_triggers`), keeps rows whose
    ``gate`` is in :data:`NARRATION_ACTUATION_GATES`, and classifies each:

    * ``detections`` — every narration row (a detected narration problem that
      reached an actuator).
    * ``actuations`` — rows whose action is a durable act (``directive_set`` or
      ``handoff``).
    * ``dropped`` — rows whose offer lost the slot race (``action == "dropped"``).
    * ``settled`` — *actuation* rows whose grounding, parsed from ``detail``
      (``#NNNN`` PR numbers via :func:`extract_issue_numbers`, ``sha=<hex>`` via
      :data:`_SHA_DETAIL_RE`), the injected oracles now show merged/landed. An
      actuation row with no parseable grounding is excluded from the settlement
      numerator — it can never be shown settled.

    *is_pr_merged* and *is_sha_landed* are the git-fact oracles (host-first PR
    merge check and SHA-ancestry check), injected so the function stays pure
    given its inputs — matching :func:`measure_unlanded_remediations`. Both are
    memoized per call, so a grounding that recurs across many rows (a still-
    unlanded SHA/PR) is probed at most once per measurement — the SHA oracle
    (``is_branch_merged_into_main``) shells out to ``git merge-base`` per call.
    Returns::

        {
            "detections": int,
            "actuations": int,
            "dropped": int,
            "settled": int,
            "per_gate": {
                <gate>: {"detections": int, "actuations": int,
                         "dropped": int, "settled": int},
                ...
            },
        }

    Returns all-zero with an empty ``per_gate`` when the trace is empty or holds
    no narration rows. Pure read given its oracles — no mutations, zero SDK cost.

    The *n*-window bounds the last *n* well-formed rows (``read_all()`` already
    drops corrupt rows), not the last *n* raw lines the pre-fold reader tailed
    before parsing — identical on a clean store, differing only by dropped-row
    slots on a crash-torn tail (proposal 106).
    """
    empty: dict[str, object] = {
        "detections": 0,
        "actuations": 0,
        "dropped": 0,
        "settled": 0,
        "per_gate": {},
    }
    records = backend.read_all()

    tail = records[-n:] if n > 0 else []
    # Memoize both oracles for the span of this single measurement. The same
    # unlanded PR/SHA can ground many actuation rows across the tail window, and
    # the injected SHA oracle (``is_branch_merged_into_main``) spawns a fresh
    # ``git merge-base`` subprocess (10s timeout) per call — so probe each
    # distinct grounding at most once per cycle instead of once per row (r1).
    pr_memo: dict[int, bool] = {}
    sha_memo: dict[str, bool] = {}

    def _pr_merged(pr: int) -> bool:
        if pr not in pr_memo:
            pr_memo[pr] = is_pr_merged(pr)
        return pr_memo[pr]

    def _sha_landed(sha: str) -> bool:
        if sha not in sha_memo:
            sha_memo[sha] = is_sha_landed(sha)
        return sha_memo[sha]

    detections = 0
    actuations = 0
    dropped = 0
    settled = 0
    per_gate: dict[str, dict[str, int]] = {}
    for record in tail:
        gate = record.gate
        if not (isinstance(gate, str) and gate in NARRATION_ACTUATION_GATES):
            continue
        action = record.action
        detail = record.detail
        bucket = per_gate.setdefault(
            gate,
            {"detections": 0, "actuations": 0, "dropped": 0, "settled": 0},
        )
        detections += 1
        bucket["detections"] += 1
        if action == "dropped":
            dropped += 1
            bucket["dropped"] += 1
        elif action in _NARRATION_ACTUATION_ACTIONS:
            actuations += 1
            bucket["actuations"] += 1
            if isinstance(detail, str) and _narration_grounding_settled(
                detail, _pr_merged, _sha_landed,
            ):
                settled += 1
                bucket["settled"] += 1

    if not per_gate:
        return dict(empty)
    return {
        "detections": detections,
        "actuations": actuations,
        "dropped": dropped,
        "settled": settled,
        "per_gate": per_gate,
    }


def format_narration_conversion_summary(
    backend: GateActivationBackend,
    *,
    is_pr_merged: Callable[[int], bool],
    is_sha_landed: Callable[[str], bool],
    n: int = 100,
) -> str:
    """Render a ``NARRATION-CONVERSION`` line, or ``""`` when no rows exist.

    Calls :func:`measure_narration_conversion` and renders one line naming the
    totals and the per-gate detections/actuations/settled breakdown, e.g.::

        NARRATION-CONVERSION: 12 detections → 9 actuations (3 dropped) → 5
        settled [unconfirmed_landing_forced 4/3/2, …]

    Returns ``""`` when the trace holds no narration rows, so an empty history
    injects nothing. Pure given its injected oracles; zero SDK cost.
    """
    result = measure_narration_conversion(
        backend, n, is_pr_merged=is_pr_merged, is_sha_landed=is_sha_landed,
    )
    detections = result["detections"]
    if not isinstance(detections, int) or detections == 0:
        return ""
    per_gate = result["per_gate"]
    parts: list[str] = []
    if isinstance(per_gate, dict):
        for gate in sorted(per_gate):
            counts = per_gate[gate]
            parts.append(
                f"{gate} {counts['detections']}/{counts['actuations']}"
                f"/{counts['settled']}"
            )
    breakdown = f" [{', '.join(parts)}]" if parts else ""
    return (
        f"NARRATION-CONVERSION: {detections} detections → "
        f"{result['actuations']} actuations ({result['dropped']} dropped) → "
        f"{result['settled']} settled{breakdown}"
    )


# ---------------------------------------------------------------------------
# Outcome prediction tracker — shape-based predictions with calibration
# ---------------------------------------------------------------------------

# Filename used by :func:`record_prediction` and :func:`measure_prediction_accuracy`.
PREDICTIONS_FILENAME = "predictions.jsonl"

# ``source`` tags distinguishing priced-reflection self-predictions (and their
# deferred verdicts) from the task-outcome predictions written by
# :func:`record_prediction`.  Reflection rows are scored by an explicit verdict,
# not by joining to a task outcome.
REFLECTION_PREDICTION_SOURCE = "reflection"
REFLECTION_VERDICT_SOURCE = "reflection_verdict"

# The ``reflection-{iteration}`` prediction-id shape has one owner: this prefix
# plus :func:`reflection_bet_cycle`, which recovers the iteration a bet was placed
# on so the calibration scorer can compute ``cycles_elapsed``. ``agent.contemplate``
# builds the id from this prefix; the scorer parses it back.
REFLECTION_PREDICTION_ID_PREFIX = "reflection-"

# Thresholds for :func:`predict_task_outcome`. Shape-specific success rate
# determines the predicted outcome: ≥0.7 → success, ≥0.4 → partial, else failure.
_PREDICTION_SUCCESS_THRESHOLD = 0.70
_PREDICTION_PARTIAL_THRESHOLD = 0.40

# Minimum matched pairs before :func:`format_known_state_summary` surfaces
# prediction accuracy in the task-generation prompt.
_PREDICTION_MIN_MATCHED = 5


def predict_task_outcome(
    tasks: list[dict[str, object]],
    goal: str,
) -> TaskOutcome:
    """Predict task outcome based on shape-specific historical success rate.

    Classifies *goal* via :func:`classify_task_shape`, computes the
    success rate for that shape across *tasks* (fraction whose ``outcome``
    equals ``"success"``), and returns:

    - ``"success"`` when rate ≥ 0.7
    - ``"partial"`` when rate ≥ 0.4
    - ``"failure"`` otherwise

    Falls back to ``"partial"`` when no historical tasks match the shape
    (empty history or no tasks with that shape). Pure function — no I/O.

    The return type is bound to :data:`~maestro.types.TaskOutcome` rather
    than a free-form ``str`` so mypy rejects a literal outside the recorded
    outcome vocabulary: :func:`measure_prediction_accuracy` joins this value
    against ``TaskRecord.outcome`` by bare equality, so a spelling that
    production never writes would score every correct prediction as wrong
    (#2919).
    """
    shape = classify_task_shape(goal)
    shape_tasks = []
    for t in tasks:
        g = t.get("goal")
        t_shape = classify_task_shape(g if isinstance(g, str) else "")
        if t_shape == shape:
            shape_tasks.append(t)
    if not shape_tasks:
        return "partial"
    successes = sum(
        1 for t in shape_tasks if t.get("outcome") == "success"
    )
    rate = successes / len(shape_tasks)
    if rate >= _PREDICTION_SUCCESS_THRESHOLD:
        return "success"
    if rate >= _PREDICTION_PARTIAL_THRESHOLD:
        return "partial"
    return "failure"


def record_prediction(
    backend: PredictionBackend,
    task_id: str,
    predicted: TaskOutcome,
) -> None:
    """Append a task-outcome prediction row via *backend*.

    *predicted* is bound to :data:`~maestro.types.TaskOutcome` — the same
    vocabulary :func:`predict_task_outcome` returns — so mypy rejects an
    off-vocabulary literal at the seam that makes the value durable, where
    :func:`measure_prediction_accuracy`'s bare-equality join would silently
    score it wrong forever (#2919).

    Builds the JSON row ``{task_id, predicted_outcome, timestamp}`` (the
    ``datetime.now(tz=UTC)`` stamp is authored here) and delegates persistence to
    the flag-selected :class:`JsonlPredictionLedger` (default) or
    :class:`~maestro.state_store.SqlitePredictionLedger`. The JSONL adapter keeps
    the historical ``file_lock`` append; the SQLite adapter serialises through its
    own transaction.
    """
    record: dict[str, object] = {
        "task_id": task_id,
        "predicted_outcome": predicted,
        "timestamp": datetime.now(tz=UTC).isoformat(),
    }
    backend.append(record)


def record_reflection_prediction(
    backend: PredictionBackend,
    prediction_id: str,
    prediction: str,
    loss_condition: str,
    *,
    confidence: float | None = None,
    loss_condition_kind: str | None = None,
) -> None:
    """Append a priced-reflection self-prediction via *backend*.

    The row is written ``verdict="unscored"``; the next reflection scores it
    by appending a :func:`record_reflection_verdict` row keyed by the same
    *prediction_id* (deferred scoring under the append-only trace). Persistence
    is delegated to the flag-selected backend (JSONL default or ``state.db``).

    *confidence* (the stated probability the prediction holds) and
    *loss_condition_kind* (the :data:`~maestro.self_calibration.ProbeKind` the
    loss condition parsed to, or ``None`` when the grammar declined it) are the
    two additive self-calibration fields (proposal 129). Each is written into the
    row **only when not ``None``**, so a caller that omits them — every caller
    while ``[audit] self_calibration_enabled`` is off — writes a row byte-identical
    to before.
    """
    record: dict[str, object] = {
        "source": REFLECTION_PREDICTION_SOURCE,
        "prediction_id": prediction_id,
        "prediction": prediction,
        "loss_condition": loss_condition,
        "verdict": "unscored",
        "timestamp": datetime.now(tz=UTC).isoformat(),
    }
    if confidence is not None:
        record["confidence"] = confidence
    if loss_condition_kind is not None:
        record["loss_condition_kind"] = loss_condition_kind
    backend.append(record)


def record_reflection_verdict(
    backend: PredictionBackend,
    prediction_id: str,
    verdict: str,
    *,
    verdict_source: str | None = None,
    self_verdict: str | None = None,
) -> None:
    """Append a verdict scoring a prior reflection prediction via *backend*.

    *verdict* is the effective verdict the ledger records (``"correct"`` /
    ``"wrong"`` / ``"pending"`` / ``"unresolved"``, or the model's own self-grade
    on the narrated path). The row overrides the matching prediction row's own
    ``verdict`` field for :func:`load_latest_reflection_prediction`,
    :func:`load_open_reflection_bets`, and :func:`measure_prediction_accuracy`.
    Persistence is delegated to the flag-selected backend (JSONL default or
    ``state.db``).

    *verdict_source* (``"measured"`` when the calibration oracle authored the
    verdict, ``"narrated"`` when the self-grade was adopted for an unfalsifiable
    bet) and *self_verdict* (the model's ``PRIOR VERDICT:`` self-grade retained
    beside a measured verdict) are the two additive calibration-authority fields
    (proposal 129). Each is written **only when not ``None``** — the same additive
    pattern :func:`record_reflection_prediction` uses for ``confidence`` /
    ``loss_condition_kind`` — so a caller that omits them (every caller while
    ``[audit] calibration_verdict_authority_enabled`` is off) writes a row
    byte-identical to before.
    """
    record: dict[str, object] = {
        "source": REFLECTION_VERDICT_SOURCE,
        "prediction_id": prediction_id,
        "verdict": verdict,
        "timestamp": datetime.now(tz=UTC).isoformat(),
    }
    if verdict_source is not None:
        record["verdict_source"] = verdict_source
    if self_verdict is not None:
        record["self_verdict"] = self_verdict
    backend.append(record)


def _read_prediction_rows(path: Path) -> list[dict[str, object]]:
    """Read and JSON-decode every row of ``predictions.jsonl``.

    Skips blank and malformed lines. Returns ``[]`` on a missing file or read
    error. Pure read — no mutations.
    """
    if not path.exists():
        return []
    rows: list[dict[str, object]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    rows.append(record)
    except OSError:
        return []
    return rows


@runtime_checkable
class PredictionBackend(Protocol):
    """The prediction-ledger persistence surface (proposal 106).

    The three heterogeneous prediction row shapes — task-outcome predictions
    (:func:`record_prediction`), priced-reflection self-predictions
    (:func:`record_reflection_prediction`), and their deferred verdicts
    (:func:`record_reflection_verdict`) — are all read back by
    :func:`load_latest_reflection_prediction`, :func:`measure_prediction_accuracy`,
    the data-sufficiency instrument, and the ``measure-fork-authorship`` CLI. So
    the ledger can live in ``predictions.jsonl`` (default) or in ``state.db``
    behind ``[paths] sqlite_state_enabled`` with no call-site branching. A pure
    append trace with no keyed-replace or retraction shape: one row per write,
    append-only, oldest-first load. Each row is stored verbatim as a JSON object
    (``dict[str, object]``) — not a fixed dataclass — so additive fields never
    force a schema migration. Named :meth:`append` / :meth:`read_all` to match the
    gate-activation trace fold this one mirrors verb-for-verb.
    ``@runtime_checkable`` enables a structural ``isinstance`` check in the
    backend-selection tests.
    """

    def append(self, record: dict[str, object]) -> None:
        """Append exactly one prediction row."""
        ...

    def read_all(self) -> list[dict[str, object]]:
        """Return every recorded prediction row oldest-first."""
        ...


def load_all_predictions(data_dir: Path) -> list[dict[str, object]]:
    """Parse every well-formed row from ``predictions.jsonl`` oldest-first.

    The shared reader — the SQLite seed source and the JSONL adapter's
    :meth:`~JsonlPredictionLedger.read_all` — delegating to the existing
    corrupt-tolerant :func:`_read_prediction_rows`: it **skips corrupt rows and
    never raises** (a malformed line anywhere, interior or trailing, is dropped,
    not propagated), and returns ``[]`` on a missing file or an ``OSError``. Order
    is oldest-first, so ``read_all()[-n:]`` is the tail window
    :func:`measure_prediction_accuracy` takes.
    """
    return _read_prediction_rows(data_dir / PREDICTIONS_FILENAME)


class JsonlPredictionLedger:
    """JSONL-backed :class:`PredictionBackend` — the default, flag-off store.

    Binds a ``data_dir`` and keeps the ledger on ``predictions.jsonl``
    (behaviour-preserving). :meth:`append` reproduces the exact pre-fold write —
    an advisory :func:`~maestro.fileutil.file_lock` around an
    ``open(path, "a", encoding="utf-8")`` append of ``json.dumps(record) + "\\n"``
    (parent-dir-created) — so the JSONL substrate is bit-for-bit unchanged, and
    concurrent appends never interleave partial lines. :meth:`read_all` reuses the
    corrupt-tolerant forward parse via :func:`load_all_predictions`.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._history_path = data_dir / PREDICTIONS_FILENAME

    def append(self, record: dict[str, object]) -> None:
        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        with (
            file_lock(self._history_path),
            open(self._history_path, "a", encoding="utf-8") as f,
        ):
            f.write(json.dumps(record) + "\n")

    def read_all(self) -> list[dict[str, object]]:
        return load_all_predictions(self._data_dir)


def load_latest_reflection_prediction(
    backend: PredictionBackend,
    *,
    before_cycle: int | None = None,
) -> dict[str, str] | None:
    """Return the most recent reflection self-prediction, or ``None``.

    The returned dict carries ``prediction_id``, ``prediction``,
    ``loss_condition`` and the *effective* ``verdict`` — a later
    ``reflection_verdict`` row keyed by the same ``prediction_id`` overrides the
    prediction row's own ``"unscored"`` default. Reads the whole trace via
    *backend*. Pure read — no mutations.

    *before_cycle*, when set, skips any ``reflection-N`` row with ``N >=
    before_cycle`` so a resumed-in-place contemplation never mistakes the bet its
    own first attempt placed this iteration for last cycle's (#4106). A row whose
    id carries no parseable cycle is kept, as before.
    """
    rows = backend.read_all()
    verdict_overrides: dict[str, str] = {}
    latest: dict[str, object] | None = None
    for record in rows:
        source = record.get("source")
        if source == REFLECTION_VERDICT_SOURCE:
            pid = record.get("prediction_id")
            verdict = record.get("verdict")
            if isinstance(pid, str) and isinstance(verdict, str):
                verdict_overrides[pid] = verdict
        elif source == REFLECTION_PREDICTION_SOURCE:
            if before_cycle is not None:
                row_pid = record.get("prediction_id")
                cycle = (
                    reflection_bet_cycle(row_pid)
                    if isinstance(row_pid, str) else None
                )
                if cycle is not None and cycle >= before_cycle:
                    continue
            latest = record
    if latest is None:
        return None
    pid = latest.get("prediction_id")
    own_verdict = latest.get("verdict")
    prediction = latest.get("prediction")
    loss_condition = latest.get("loss_condition")
    effective = "unscored"
    if isinstance(own_verdict, str) and own_verdict:
        effective = own_verdict
    if isinstance(pid, str) and pid in verdict_overrides:
        effective = verdict_overrides[pid]
    return {
        "prediction_id": pid if isinstance(pid, str) else "",
        "prediction": prediction if isinstance(prediction, str) else "",
        "loss_condition": (
            loss_condition if isinstance(loss_condition, str) else ""
        ),
        "verdict": effective,
    }


def has_reflection_prediction(
    backend: PredictionBackend, prediction_id: str,
) -> bool:
    """Return whether a reflection prediction row keyed *prediction_id* exists.

    Pure read. ``contemplate`` consults it before registering its
    ``reflection-{iteration}`` bet so a phase re-run at the same iteration keeps
    the first attempt's bet rather than appending a colliding second row (#4106).
    """
    return any(
        record.get("source") == REFLECTION_PREDICTION_SOURCE
        and record.get("prediction_id") == prediction_id
        for record in backend.read_all()
    )


def reflection_bet_cycle(prediction_id: str) -> int | None:
    """Recover the iteration a ``reflection-{iteration}`` bet was placed on.

    Returns the integer cycle when *prediction_id* has the
    :data:`REFLECTION_PREDICTION_ID_PREFIX` shape with a non-negative integer
    suffix, else ``None`` — a malformed or foreign id declines rather than raising,
    so the scorer skips a corrupt ledger row instead of crashing on it.
    """
    if not prediction_id.startswith(REFLECTION_PREDICTION_ID_PREFIX):
        return None
    suffix = prediction_id[len(REFLECTION_PREDICTION_ID_PREFIX):]
    if not suffix.isdigit():
        return None
    return int(suffix)


@dataclass(frozen=True)
class _JoinedReflectionRow:
    """One reflection self-prediction joined to its effective verdict.

    The value object the single-pass reflection join (:func:`_join_reflection_rows`)
    yields, one per ``reflection`` prediction row: the prediction's own fields plus
    the *effective* verdict (a later ``reflection_verdict`` override keyed by the
    same ``prediction_id`` wins over the prediction row's ``"unscored"`` default)
    and the four additive calibration fields the readers key off — ``verdict_source``
    (``"measured"`` / ``"narrated"`` when the winning override stamped one, else
    ``None``), ``self_verdict`` (the model's ``PRIOR VERDICT:`` self-grade, ``""``
    when none), ``confidence`` (the stored value verbatim — an ``object`` because a
    hand-edited row can carry ``"0.8"`` or ``null``; parse it with
    :func:`_parse_stored_confidence`), and ``loss_condition_kind`` (the
    :class:`~maestro.self_calibration.ProbeKind` the loss condition parsed to, or
    ``None``). Non-string fields collapse to ``""`` the same way the pre-extraction
    readers did, so a corrupt row never raises.
    """

    prediction_id: str
    prediction: str
    loss_condition: str
    timestamp: str
    verdict: str
    verdict_source: str | None
    self_verdict: str
    confidence: object
    # Surfaced by the join but read by no consumer in this increment: carried for
    # the forthcoming per-family calibration breakdown (proposal 129 forward-carry).
    loss_condition_kind: str | None


def _join_reflection_rows(
    rows: list[dict[str, object]],
) -> list[_JoinedReflectionRow]:
    """Join every ``reflection`` prediction in *rows* to its effective verdict.

    The single-pass join the three reflection readers historically each open-coded
    (:func:`load_latest_reflection_prediction`, :func:`load_open_reflection_bets`,
    :func:`measure_prediction_accuracy`): split *rows* on ``source`` against
    :data:`REFLECTION_PREDICTION_SOURCE` / :data:`REFLECTION_VERDICT_SOURCE`,
    collect the last verdict override (its ``verdict``, ``verdict_source``, and
    ``self_verdict``) keyed by ``prediction_id``, then resolve each prediction's
    ``effective = override else the row's own verdict``. Extracting it once removes
    the drift hazard of a fourth copy (the calibration read-back, proposal 129) and
    surfaces ``verdict_source`` / ``confidence`` / ``loss_condition_kind`` — the
    calibration fields the older open-coded joins ignored — on one value object.

    *rows* is the caller's chosen window (``backend.read_all()`` whole, or a
    ``[-n:]`` tail), so this helper invents **no** windowing rule of its own.
    Predictions are returned oldest-first (source order). Pure read — no mutations.
    """
    verdict_overrides: dict[str, str] = {}
    verdict_source_overrides: dict[str, str | None] = {}
    self_verdict_overrides: dict[str, str] = {}
    predictions: list[dict[str, object]] = []
    for record in rows:
        source = record.get("source")
        if source == REFLECTION_VERDICT_SOURCE:
            pid = record.get("prediction_id")
            verdict = record.get("verdict")
            if isinstance(pid, str) and isinstance(verdict, str):
                verdict_overrides[pid] = verdict
                stamp = record.get("verdict_source")
                verdict_source_overrides[pid] = (
                    stamp if isinstance(stamp, str) else None
                )
            self_verdict = record.get("self_verdict")
            if isinstance(pid, str) and isinstance(self_verdict, str):
                self_verdict_overrides[pid] = self_verdict
        elif source == REFLECTION_PREDICTION_SOURCE:
            predictions.append(record)
    joined: list[_JoinedReflectionRow] = []
    for record in predictions:
        pid = record.get("prediction_id")
        own_verdict = record.get("verdict")
        prediction = record.get("prediction")
        loss_condition = record.get("loss_condition")
        timestamp = record.get("timestamp")
        loss_condition_kind = record.get("loss_condition_kind")
        effective = "unscored"
        if isinstance(own_verdict, str) and own_verdict:
            effective = own_verdict
        source_stamp: str | None = None
        self_grade = ""
        if isinstance(pid, str):
            if pid in verdict_overrides:
                effective = verdict_overrides[pid]
                source_stamp = verdict_source_overrides.get(pid)
            self_grade = self_verdict_overrides.get(pid, "")
        joined.append(_JoinedReflectionRow(
            prediction_id=pid if isinstance(pid, str) else "",
            prediction=prediction if isinstance(prediction, str) else "",
            loss_condition=(
                loss_condition if isinstance(loss_condition, str) else ""
            ),
            timestamp=timestamp if isinstance(timestamp, str) else "",
            verdict=effective,
            verdict_source=source_stamp,
            self_verdict=self_grade,
            confidence=record.get("confidence"),
            loss_condition_kind=(
                loss_condition_kind
                if isinstance(loss_condition_kind, str) else None
            ),
        ))
    return joined


def load_open_reflection_bets(
    backend: PredictionBackend,
) -> list[dict[str, str]]:
    """Return every still-open reflection self-prediction, oldest-first.

    The plural sibling of :func:`load_latest_reflection_prediction`: reading through
    the shared :func:`_join_reflection_rows` join (a later ``reflection_verdict``
    row keyed by the same ``prediction_id`` overrides the prediction row's
    ``"unscored"`` default), it keeps *every* prediction whose effective verdict is
    still ``"unscored"`` or ``"pending"`` — not just the latest. A multi-cycle bet
    therefore stays reachable after newer bets displace it as "latest", so the
    calibration scorer can re-score it each cycle until its window matures. Each
    returned dict carries ``prediction_id``, ``prediction``, ``loss_condition``, the
    prediction row's ``timestamp`` (the window start the scorer filters evidence
    against), the effective ``verdict``, and ``self_verdict`` — the model's ``PRIOR
    VERDICT:`` self-grade last persisted for this bet (empty string when none),
    carried so a bet that stops being "latest" before its window matures can still
    recover the self-grade for the measured-vs-narrated divergence trace. Pure read
    — no mutations.
    """
    open_bets: list[dict[str, str]] = []
    for row in _join_reflection_rows(backend.read_all()):
        if row.verdict not in ("unscored", "pending"):
            continue
        open_bets.append({
            "prediction_id": row.prediction_id,
            "prediction": row.prediction,
            "loss_condition": row.loss_condition,
            "timestamp": row.timestamp,
            "verdict": row.verdict,
            "self_verdict": row.self_verdict,
        })
    return open_bets


def banned_stale_loss_conditions(
    backend: PredictionBackend, *, threshold: int = 2,
) -> frozenset[str]:
    """Fold the whole reflection ledger into the stale-repeat ban set (composer).

    The thin composer ``agent.contemplate`` calls behind
    ``[loop] stale_repeat_bet_guard_enabled``: it reads every reflection
    prediction row (open bets plus scored bets together cover the full ledger —
    :func:`load_open_reflection_bets` keeps the ``unscored``/``pending`` rows,
    :func:`load_scored_reflection_bets` with ``n<=0`` keeps the rest) and hands
    them to the pure
    :func:`~maestro.self_calibration.stale_repeat_loss_conditions`, returning the
    normalised loss-condition strings already staked at or above *threshold* times
    without ever scoring ``"correct"`` (the comfort-bet signature). Lives here,
    beside the loaders it composes, so the priced-prompt caller stays a one-liner.
    """
    rows = (
        load_open_reflection_bets(backend)
        + load_scored_reflection_bets(backend, 0)
    )
    return frozenset(stale_repeat_loss_conditions(rows, threshold=threshold))


def stale_repeat_introspect_kwargs(
    backend: PredictionBackend, *, enabled: bool,
) -> dict[str, frozenset[str]]:
    """Build the priced-``introspect`` banned-loss-condition kwargs (composer).

    The one-liner ``agent.contemplate`` calls behind
    ``[loop] stale_repeat_bet_guard_enabled``: returns ``{}`` when the guard is
    off *or* no loss condition is stale-repeat-banned (so the priced prompt stays
    byte-identical to the pre-guard path), else
    ``{"banned_loss_conditions": <set>}`` folded from
    :func:`banned_stale_loss_conditions`. Lives here beside that composer so the
    ``agent.py`` caller stays a single expression. Zero SDK cost.
    """
    if not enabled:
        return {}
    banned = banned_stale_loss_conditions(backend)
    if not banned:
        return {}
    return {"banned_loss_conditions": banned}


def load_scored_reflection_bets(
    backend: PredictionBackend, n: int = 20,
) -> list[dict[str, str]]:
    """Return the *scored* reflection self-predictions, oldest-first (proposal 130).

    The complement of :func:`load_open_reflection_bets`: reading through the same
    :func:`_join_reflection_rows` join, it keeps every prediction whose effective
    verdict is **not** still ``"unscored"`` or ``"pending"`` — a bet the
    calibration oracle (or the prediction row itself) has already graded. Each
    returned dict carries ``prediction_id``, ``prediction``, ``loss_condition``,
    the prediction row's ``timestamp``, the effective ``verdict``,
    ``verdict_source`` (``"measured"`` / ``"narrated"``, or ``""`` when the
    winning verdict stamped none — the authorship warrant the conscious-access
    adapter filters on) and ``self_verdict``. The join runs over the **whole**
    ledger and *n* then bounds the *result* to the last *n* scored bets (all of
    them when *n* is not positive): windowing the raw rows instead would drop a
    bet whose prediction row has aged out of the tail even though its verdict row
    is inside it, so a just-scored bet could vanish. Pure read — no mutations.
    """
    scored: list[dict[str, str]] = []
    for row in _join_reflection_rows(backend.read_all()):
        if row.verdict in ("unscored", "pending"):
            continue
        scored.append({
            "prediction_id": row.prediction_id,
            "prediction": row.prediction,
            "loss_condition": row.loss_condition,
            "timestamp": row.timestamp,
            "verdict": row.verdict,
            "verdict_source": row.verdict_source or "",
            "self_verdict": row.self_verdict,
        })
    return scored[-n:] if n > 0 else scored


# Bounded tail reads feeding the calibration-verdict scorer (proposal 129). When a
# reflection bet of the matching probe kind is open, ``build_calibration_evidence``
# reads at most this many recent rows of each source and filters them to the bet's
# window. The caps are cost bounds only (context-bloat cap): coverage is measured
# per bet from the oldest row read (``_tail_spans_since``), so a tail crowded past
# the bet's ``since`` leaves the bet uncovered (``pending`` then ``unresolved``),
# never a confident negative. Gate activations fire far more often than tasks, hence the larger tail; sensor
# readings are a few per cycle.
_CALIBRATION_TASK_WINDOW = 100
_CALIBRATION_GATE_WINDOW = 500
_CALIBRATION_SENSOR_WINDOW = 500


def _tail_spans_since(oldest_timestamp: str | None, since: str) -> bool:
    """True iff a non-empty oldest-first tail starts strictly before *since*.

    Only then does the bounded read provably span the bet's window start, so an
    absent in-window match is genuine negative evidence. A short read is not
    treated as exhaustive (a corrupt-tail skip budget can also cut it short).
    """
    return oldest_timestamp is not None and oldest_timestamp < since


_WINDOWED_PROBE_KINDS = frozenset({"TASK_OUTCOME", "GATE_FIRING", "SENSOR_THRESHOLD"})


def calibration_window_ends(
    probes: dict[str, LossProbe | None],
    iteration_starts: dict[int, str],
    *,
    current_iteration: int,
    current_started_at: str,
) -> dict[str, str | None]:
    """Resolve each windowed bet's exclusive wall-clock window end.

    A ``reflection-{cycle}`` bet's window closes with iteration
    ``cycle + matures_after_cycles``, so its exclusive bound is the
    ``started_at`` of iteration ``cycle + window.closes_at_elapsed`` — the one
    owner (:attr:`ObservationWindow.closes_at_elapsed`) the scorer also reads. The value per ``prediction_id``:
    ``""`` — the window is still open (the bound iteration has not begun), no
    upper bound; a timestamp — the bound (the in-flight iteration's own
    *current_started_at* when it is the bound, since its ``usage.jsonl`` row is
    not written yet); ``None`` — the bound iteration's start is unknown
    (archived, frozen counter, corrupt row), so the bet must not be scored on
    covered evidence. Non-windowed or unparseable bets are omitted.
    """
    ends: dict[str, str | None] = {}
    for pid, probe in probes.items():
        cycle = reflection_bet_cycle(pid)
        if probe is None or probe.kind not in _WINDOWED_PROBE_KINDS or cycle is None:
            continue
        bound = cycle + probe.window.closes_at_elapsed
        if bound > current_iteration:
            ends[pid] = ""
        elif bound == current_iteration:
            # A frozen counter may already have flushed rows for this
            # iteration; its earliest recorded start is the true bound.
            starts = [t for t in (iteration_starts.get(bound), current_started_at) if t]
            ends[pid] = min(starts) if starts else None
        else:
            ends[pid] = iteration_starts.get(bound)
    return ends


def build_calibration_evidence(
    open_bets: list[dict[str, str]],
    memory: Memory,
    gate_activations: GateActivationBackend,
    *,
    current_iteration: int,
    current_started_at: str,
) -> dict[str, CalibrationEvidence]:
    """Build one :class:`CalibrationEvidence` bundle per open reflection bet.

    Reads each source (task history, the gate-activation tail, the plan store, the
    sensor-readings sidecar, the ``usage.jsonl`` iteration starts) *at most once*
    and *only when needed*, so a cycle with no bets of a kind adds zero reads.
    Task, gate and sensor rows are filtered to each bet's two-ended window,
    ``since <= row.timestamp < until``: ``since`` is the bet's ``timestamp`` and
    ``until`` the start of the iteration after the window closes
    (:func:`calibration_window_ends`), so a bet scored late — ``contemplate``
    runs every few iterations, and a counter freeze lets wall-clock pass — sees
    only in-window rows and scores as if scored on time. Task/gate windows are
    marked ``*_window_covered`` only when the bounded tail's oldest row predates
    ``since`` (so an empty window reads as genuine negative evidence; a crowded
    tail stays uncovered); a bet whose ``since`` is empty (corrupt row) or whose ``until``
    cannot be resolved carries no evidence at all, so it resolves ``pending``
    then ``unresolved`` rather than admitting an unbounded tail. An inverted
    window (``until < since``) raises :class:`ValueError`. The plan store is
    injected as the current ``(plan_id, status)`` snapshot (no status history is
    kept to bound it). PR settlement is still not collected this increment.
    Keyed by ``prediction_id``. Zero SDK cost — pure ledger reads.
    """
    probes: dict[str, LossProbe | None] = {
        bet["prediction_id"]: parse_loss_condition(bet["loss_condition"])
        for bet in open_bets
    }
    kinds_present = {p.kind for p in probes.values() if p is not None}

    closed_cycles = [
        cycle
        for pid, p in probes.items()
        if p is not None
        and p.kind in _WINDOWED_PROBE_KINDS
        and (cycle := reflection_bet_cycle(pid)) is not None
        and cycle + p.window.closes_at_elapsed <= current_iteration
    ]
    # Size the read from the oldest closed bet's age, not a constant: the
    # contemplate cadence widens the scoreable age (``max_scoreable_age``), so a
    # fixed span would under-read a bound iteration at a slow cadence.
    iteration_starts = (
        memory.get_iteration_starts(
            max(2 * MAX_SCOREABLE_AGE_CYCLES, current_iteration - min(closed_cycles) + 1)
        )
        if closed_cycles else {}
    )
    window_ends = calibration_window_ends(
        probes,
        iteration_starts,
        current_iteration=current_iteration,
        current_started_at=current_started_at,
    )

    task_rows = (
        memory.get_recent_tasks(_CALIBRATION_TASK_WINDOW)
        if "TASK_OUTCOME" in kinds_present else []
    )
    gate_rows = (
        gate_activations.read_tail(_CALIBRATION_GATE_WINDOW)
        if "GATE_FIRING" in kinds_present else []
    )
    plan_statuses: tuple[tuple[str, str], ...] = ()
    if "PLAN_STATUS" in kinds_present:
        plan_statuses = tuple(
            (plan.plan_id, plan.status) for plan in memory.get_all_plans()
        )
    sensor_rows = (
        memory.get_recent_sensor_readings(_CALIBRATION_SENSOR_WINDOW)
        if "SENSOR_THRESHOLD" in kinds_present else []
    )

    evidence: dict[str, CalibrationEvidence] = {}
    for bet in open_bets:
        pid = bet["prediction_id"]
        probe = probes.get(pid)
        since = bet.get("timestamp", "")
        until = window_ends.get(pid, "")
        if until and since and until < since:
            raise ValueError(
                f"inverted calibration window for {pid}: until {until} < since {since}"
            )

        def in_window(ts: str, since: str = since, until: str | None = until) -> bool:
            return ts >= since and (not until or ts < until)

        if probe is None or until is None:
            # Unparseable, or a matured window whose end iteration's start is
            # unknown: no bounded window, so no (covered) evidence.
            evidence[pid] = CalibrationEvidence()
        elif probe.kind == "TASK_OUTCOME" and since:
            evidence[pid] = CalibrationEvidence(
                task_outcomes=tuple(
                    (classify_task_shape(row.goal), row.outcome)
                    for row in task_rows
                    if in_window(row.timestamp)
                ),
                task_window_covered=_tail_spans_since(
                    task_rows[0].timestamp if task_rows else None, since,
                ),
            )
        elif probe.kind == "GATE_FIRING" and since:
            evidence[pid] = CalibrationEvidence(
                fired_gates=tuple(
                    row.gate for row in gate_rows if in_window(row.timestamp)
                ),
                gate_window_covered=_tail_spans_since(
                    gate_rows[0].timestamp if gate_rows else None, since,
                ),
            )
        elif probe.kind == "PLAN_STATUS":
            evidence[pid] = CalibrationEvidence(plan_statuses=plan_statuses)
        elif probe.kind == "SENSOR_THRESHOLD" and since:
            # Every in-window reading; the scorer filters by tag and decides
            # crossing against the bet's own threshold. An empty ``since`` falls
            # to the else-arm below.
            evidence[pid] = CalibrationEvidence(
                sensor_readings=tuple(
                    (row.tag, row.value)
                    for row in sensor_rows
                    if in_window(row.timestamp)
                ),
            )
        else:
            # PR_SETTLEMENT carries no evidence this increment; a windowed
            # (TASK_OUTCOME / GATE_FIRING / SENSOR_THRESHOLD) bet with an empty
            # ``since`` (corrupt row) falls here too rather than admitting the
            # whole tail as covered (``row >= ""`` is always true) — with no
            # usable window start it is not negative evidence, so it resolves
            # ``pending`` then ``unresolved`` instead of being mis-scored.
            evidence[pid] = CalibrationEvidence()
    return evidence


def measure_prediction_accuracy(
    backend: PredictionBackend,
    task_history: list[dict[str, object]],
    n: int = 20,
) -> dict[str, object]:
    """Measure prediction accuracy against actual task outcomes.

    Reads the last *n* predictions from the flag-selected *backend*, joins to
    *task_history* by ``task_id`` on the ``outcome`` field, and returns::

        {
            "total": int,       # matched pairs (prediction + completed task)
            "correct": int,     # pairs where predicted == actual outcome
            "accuracy": float,  # correct / total, 0.0 when total == 0
            "by_shape": {
                <shape>: {"total": int, "correct": int},
                ...
            },
            "by_verdict_source": {
                <"measured"|"narrated"|"unstamped">: {"total": int, "correct": int},
                ...
            },
        }

    Tasks not yet completed (no matching ``task_id`` in *task_history* or
    missing ``outcome``) are excluded from the count. ``by_verdict_source`` is an
    additive split (proposal 129) of the *reflection* rows folded into the count,
    bucketed by their winning verdict override's ``verdict_source`` stamp —
    ``"measured"`` (the calibration oracle authored it), ``"narrated"`` (a
    self-grade adopted for an unfalsifiable bet), or ``"unstamped"`` (a row written
    before verdict authority, or scored by the prediction row's own verdict); it is
    always ``{}`` for a task-outcome-only ledger. Every prior key keeps its exact
    meaning, so the ``generate_task`` call site — which reads only ``total`` and
    ``accuracy`` — renders identically. Returns
    ``{"total": 0, "correct": 0, "accuracy": 0.0, "by_shape": {},
    "by_verdict_source": {}}`` when the backend has no rows.

    Pure read — no mutations.
    """
    empty: dict[str, object] = {
        "total": 0,
        "correct": 0,
        "accuracy": 0.0,
        "by_shape": {},
        "by_verdict_source": {},
    }

    # Read the whole trace oldest-first and take the last n valid rows. Since
    # ``read_all`` already drops corrupt/blank rows, this last-*n*-valid-rows
    # tail equals the pre-fold "last-*n* lines" tail whenever the trace has no
    # corrupt/blank tail rows (which the SQLite store cannot have).
    tail = backend.read_all()[-n:] if n > 0 else []
    predictions: list[dict[str, str]] = []
    for record in tail:
        source = record.get("source")
        # Priced-reflection self-predictions are scored by an explicit verdict,
        # not by a task-outcome join — the shared join folds them in below.
        if source in (REFLECTION_VERDICT_SOURCE, REFLECTION_PREDICTION_SOURCE):
            continue
        tid = record.get("task_id")
        predicted = record.get("predicted_outcome")
        if isinstance(tid, str) and isinstance(predicted, str):
            predictions.append({"task_id": tid, "predicted_outcome": predicted})

    # The reflection rows in the same tail, joined to their effective verdict via
    # the shared single-pass join so this reader carries no fourth copy of it.
    reflection = _join_reflection_rows(tail)

    if not predictions and not reflection:
        return empty

    # Build outcome lookup from task_history
    outcome_by_id: dict[str, str] = {}
    goal_by_id: dict[str, str] = {}
    for t in task_history:
        tid = t.get("task_id")
        outcome = t.get("outcome")
        goal = t.get("goal")
        if isinstance(tid, str) and isinstance(outcome, str):
            outcome_by_id[tid] = outcome
        if isinstance(tid, str) and isinstance(goal, str):
            goal_by_id[tid] = goal

    total = 0
    correct = 0
    by_shape: dict[str, dict[str, int]] = {}
    by_verdict_source: dict[str, dict[str, int]] = {}
    for pred in predictions:
        tid = pred["task_id"]
        actual = outcome_by_id.get(tid)
        if actual is None:
            continue
        total += 1
        predicted = pred["predicted_outcome"]
        is_correct = predicted == actual
        if is_correct:
            correct += 1

        shape = classify_task_shape(goal_by_id.get(tid, ""))
        bucket = by_shape.setdefault(shape, {"total": 0, "correct": 0})
        bucket["total"] += 1
        if is_correct:
            bucket["correct"] += 1

    # Fold in priced-reflection self-predictions. A reflection row counts once its
    # effective verdict resolves to ``"correct"`` or ``"wrong"``; an ``"unscored"``
    # / ``"pending"`` row is excluded, mirroring the task-not-yet-completed rule.
    for row in reflection:
        if row.verdict not in ("correct", "wrong"):
            continue
        total += 1
        is_correct = row.verdict == "correct"
        if is_correct:
            correct += 1
        bucket = by_shape.setdefault(
            REFLECTION_PREDICTION_SOURCE, {"total": 0, "correct": 0},
        )
        bucket["total"] += 1
        if is_correct:
            bucket["correct"] += 1
        source_key = (
            row.verdict_source
            if row.verdict_source in ("measured", "narrated")
            else "unstamped"
        )
        vbucket = by_verdict_source.setdefault(
            source_key, {"total": 0, "correct": 0},
        )
        vbucket["total"] += 1
        if is_correct:
            vbucket["correct"] += 1

    return {
        "total": total,
        "correct": correct,
        "accuracy": correct / total if total > 0 else 0.0,
        "by_shape": by_shape,
        "by_verdict_source": by_verdict_source,
    }


# ---------------------------------------------------------------------------
# Self-calibration read-back (proposal 129) — read the priced-reflection
# prediction ledger back on itself: measured hit rate, self-vs-measured
# agreement, a Brier score over the stated confidences, and the narrated /
# pending / unresolved shares. Zero SDK cost — one bounded pass over rows the
# loop already writes.
# ---------------------------------------------------------------------------

#: Every verdict the calibration oracle can stamp, derived from the closed
#: :data:`~maestro.self_calibration.CalibrationVerdict` vocabulary so a foreign or
#: hand-edited row carrying a garbage ``verdict`` on a ``"measured"`` stamp is
#: skipped rather than miscounted. ``"unfalsifiable"`` never reaches a recorded
#: row (it becomes the ``"narrated"`` fallback), so it is inert here.
_ALL_CALIBRATION_VERDICTS: frozenset[str] = frozenset(get_args(CalibrationVerdict))

#: The two verdicts that count as a *resolved* measured bet — one that landed one
#: way or the other, as opposed to ``"pending"`` / ``"unresolved"``.
_RESOLVED_VERDICTS: frozenset[str] = frozenset({"correct", "wrong"})

#: Minimum confidence-bearing measured-and-resolved bets before the Brier score
#: and its reliability bins are reported — the "enough rows to report" threshold,
#: mirroring :data:`_PREDICTION_MIN_MATCHED`. Below it the line still prints the
#: hit rate and agreement, just no (meaningless) score.
_CALIBRATION_BRIER_MIN = 5

#: Default ledger tail (in raw rows) the calibration reading windows. Wider than
#: :func:`measure_prediction_accuracy`'s window because reflection *verdict* rows
#: interleave the prediction rows, so a raw-row tail covers fewer reflection bets
#: than its length; this keeps the calibration horizon meaningful. The tail idiom
#: is ``read_all()[-n:]`` — the same one every sibling measure uses.
_CALIBRATION_WINDOW = 200

#: Coarse reliability bins for the Brier breakdown: a stated confidence is bucketed
#: by these two interior edges into low / mid / high thirds, so the rendered line
#: shows observed hits-per-bin against the confidence the model staked. Kept coarse
#: (three bins) deliberately — a thin ledger cannot populate fine ones.
_CALIBRATION_BIN_EDGES: tuple[tuple[float, str], ...] = (
    (1 / 3, "0.00-0.33"),
    (2 / 3, "0.34-0.66"),
)
_CALIBRATION_TOP_BIN_LABEL = "0.67-1.00"


def _confidence_bin(confidence: float) -> str:
    """Bucket a ``[0, 1]`` confidence into one of the coarse reliability bins."""
    for edge, label in _CALIBRATION_BIN_EDGES:
        if confidence < edge:
            return label
    return _CALIBRATION_TOP_BIN_LABEL


def _parse_stored_confidence(raw: object) -> float | None:
    """Read a verbatim-stored ``confidence`` into a clamped ``[0, 1]`` float.

    Prediction ledger rows are stored verbatim as ``dict[str, object]`` precisely
    so additive fields need no migration, so a foreign or hand-edited row can carry
    ``"0.8"`` or ``null`` in ``confidence``. Follows the usual untrusted-numeric
    idiom: coerce with ``float`` (via ``str`` so the
    ``object`` value type-checks), reject NaN/inf, and clamp to ``[0, 1]``, rather
    than trusting a bare ``isinstance`` check. Returns ``None`` for a missing,
    ``null``, or unparseable value — the bet then falls out of the Brier numerator.
    """
    if raw is None:
        return None
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return max(0.0, min(1.0, value))


@dataclass(frozen=True)
class SelfCalibrationReading:
    """The accumulated calibration summary over the reflection ledger tail.

    Returned by :func:`measure_self_calibration` and rendered by
    :func:`format_self_calibration_summary`. Follows the frozen-dataclass shape of
    :class:`OutcomeTrend` / :class:`PremiseReport` rather than a ``dict[str,
    object]`` so the formatter reads each field without a per-field cast.

    *stamped_total* — reflection rows whose winning verdict carries a
    ``verdict_source`` stamp (``"measured"`` or ``"narrated"``); the suppression
    gate, and the denominator of the three shares below.
    *narrated* — rows stamped ``"narrated"`` (the grammar could not score the bet,
    so the self-grade was adopted); its share is the design's own tripwire.
    *resolved* / *hits* / *hit_rate* — measured bets that landed
    (``correct`` / ``wrong``), those that landed ``correct``, and the rate.
    *agreement_total* / *agreements* / *agreement_rate* — measured-and-resolved rows
    carrying a ``self_verdict``, those whose self-grade matches the measured verdict,
    the rate (a still-``pending`` bet's grade is not yet comparable, so it is out of
    this domain — counting it would deflate the rate with immaturity).
    *pending* / *unresolved* — measured bets still immature / abandoned at cutoff.
    *brier_total* / *brier_score* — confidence-bearing measured-and-resolved bets
    and their mean squared error against the ``correct``/``wrong`` outcome
    (``0.0`` when none); the term that makes stated confidence loseable.
    *reliability_bins* — per confidence-bin ``{"total", "hits"}`` counts, populated
    only once *brier_total* reaches :data:`_CALIBRATION_BRIER_MIN` (else ``{}``).
    """

    stamped_total: int
    narrated: int
    resolved: int
    hits: int
    hit_rate: float
    agreement_total: int
    agreements: int
    agreement_rate: float
    pending: int
    unresolved: int
    brier_total: int
    brier_score: float
    reliability_bins: dict[str, dict[str, int]]


def measure_self_calibration(
    backend: PredictionBackend, n: int = _CALIBRATION_WINDOW,
) -> SelfCalibrationReading:
    """Read the priced-reflection prediction ledger back on itself (proposal 129).

    Over the last *n* rows (``read_all()[-n:]``; ``n <= 0`` reads nothing), joined
    to their effective verdicts via the shared :func:`_join_reflection_rows`, this
    computes the reflection's own track record: the measured hit rate over resolved
    ``correct``/``wrong`` bets, self-vs-measured agreement over resolved measured
    rows carrying a ``self_verdict``, a Brier score plus coarse reliability bins over the
    measured-and-resolved bets whose row carries a parseable ``confidence``, and the
    narrated / ``pending`` / ``unresolved`` shares of its stamped bets. A row's
    ``confidence`` is read defensively (:func:`_parse_stored_confidence`); a zero
    denominator yields ``0.0``, never a raise. Pure read — zero SDK cost.
    """
    joined = _join_reflection_rows(backend.read_all()[-n:] if n > 0 else [])
    # Suppression gate: any row carrying a verdict_source stamp at all.
    stamped_total = sum(
        1 for r in joined if r.verdict_source in ("measured", "narrated")
    )
    narrated = sum(1 for r in joined if r.verdict_source == "narrated")
    # Metric domain: measured rows carrying a recognised calibration verdict.
    measured = [
        r for r in joined
        if r.verdict_source == "measured"
        and r.verdict in _ALL_CALIBRATION_VERDICTS
    ]
    resolved_rows = [r for r in measured if r.verdict in _RESOLVED_VERDICTS]
    resolved = len(resolved_rows)
    hits = sum(1 for r in resolved_rows if r.verdict == "correct")
    hit_rate = hits / resolved if resolved else 0.0
    # Agreement is only meaningful once the measured verdict has resolved: a still
    # ``pending``/``unresolved`` bet carries the model's ``correct``/``wrong`` self
    # grade (contemplate stamps it on the first pending re-score), which can never
    # equal a ``pending`` verdict — scoring it here would deflate the rate with
    # immaturity rather than genuine miscalibration, so restrict to resolved rows.
    graded = [r for r in resolved_rows if r.self_verdict]
    agreement_total = len(graded)
    agreements = sum(1 for r in graded if r.self_verdict == r.verdict)
    agreement_rate = agreements / agreement_total if agreement_total else 0.0
    pending = sum(1 for r in measured if r.verdict == "pending")
    unresolved = sum(1 for r in measured if r.verdict == "unresolved")
    # Brier over measured-and-resolved bets carrying a parseable confidence.
    scored: list[tuple[float, int]] = []
    for r in resolved_rows:
        confidence = _parse_stored_confidence(r.confidence)
        if confidence is None:
            continue
        scored.append((confidence, 1 if r.verdict == "correct" else 0))
    brier_total = len(scored)
    brier_score = (
        sum((c - o) ** 2 for c, o in scored) / brier_total
        if brier_total else 0.0
    )
    reliability_bins: dict[str, dict[str, int]] = {}
    if brier_total >= _CALIBRATION_BRIER_MIN:
        for confidence, outcome in scored:
            bucket = reliability_bins.setdefault(
                _confidence_bin(confidence), {"total": 0, "hits": 0},
            )
            bucket["total"] += 1
            bucket["hits"] += outcome
    return SelfCalibrationReading(
        stamped_total=stamped_total,
        narrated=narrated,
        resolved=resolved,
        hits=hits,
        hit_rate=hit_rate,
        agreement_total=agreement_total,
        agreements=agreements,
        agreement_rate=agreement_rate,
        pending=pending,
        unresolved=unresolved,
        brier_total=brier_total,
        brier_score=brier_score,
        reliability_bins=reliability_bins,
    )


def format_self_calibration_summary(
    backend: PredictionBackend, n: int = _CALIBRATION_WINDOW,
) -> str:
    """Render a ``SELF-CALIBRATION`` line, or ``""`` when nothing is stamped.

    Delegates to :func:`measure_self_calibration` and returns ``""`` when no
    reflection row carries a ``verdict_source`` stamp — so the sensor extinguishes
    itself the moment there is nothing measured to report, following the
    self-suppressing formatter contract of
    :func:`format_dead_on_arrival_mints_summary` /
    :func:`format_ungrounded_rate_summary`. The Brier / reliability clause
    suppresses independently until :data:`_CALIBRATION_BRIER_MIN` confidence-bearing
    measured bets exist, so a thin ledger prints the hit rate and agreement without
    a meaningless score. Rates print as ``count/total ({x:.0%})`` — the raw fraction
    always beside the percentage — and the Brier score as ``{x:.2f}``, with per-bin
    counts in the ``", ".join(f"{k}={v}")`` style the sibling breakdowns use. Pure
    formatting — zero SDK cost, measurement-only.
    """
    reading = measure_self_calibration(backend, n)
    if reading.stamped_total <= 0:
        return ""
    total = reading.stamped_total
    line = (
        f"SELF-CALIBRATION: measured verdicts land "
        f"{reading.hits}/{reading.resolved} ({reading.hit_rate:.0%}); "
        f"self-grade vs measured agree "
        f"{reading.agreements}/{reading.agreement_total} "
        f"({reading.agreement_rate:.0%}); of {total} stamped bets "
        f"narrated {reading.narrated}/{total} ({reading.narrated / total:.0%}), "
        f"pending {reading.pending}/{total} ({reading.pending / total:.0%}), "
        f"unresolved {reading.unresolved}/{total} "
        f"({reading.unresolved / total:.0%})"
    )
    if reading.brier_total >= _CALIBRATION_BRIER_MIN:
        bins = ", ".join(
            f"{label}={counts['hits']}/{counts['total']}"
            for label, counts in sorted(reading.reliability_bins.items())
        )
        line += (
            f"; Brier {reading.brier_score:.2f} over {reading.brier_total} "
            f"confidence-bearing bets (reliability {bins})"
        )
    return (
        line
        + " — my own prediction track record; a rising narrated share means the "
        "loss-condition grammar cannot express the bets I want to make"
    )


# ---------------------------------------------------------------------------
# Data-sufficiency check — count JSONL observations per instrument file
# ---------------------------------------------------------------------------

#: Minimum number of observations (JSONL records) required before an
#: instrument is considered to have sufficient data for reliable analysis.
DATA_SUFFICIENCY_MIN_OBSERVATIONS = 10

#: Logical names of the data-sufficiency targets that may route through a
#: flag-selected backend instead of a filename count (proposal 106): the
#: gate-activation trace via a :class:`GateActivationBackend` and the prediction
#: ledger via a :class:`PredictionBackend`. The ``usage`` target stays
#: filename-counted (no backend fold yet).
_GATE_ACTIVATIONS_TARGET = "gate_activations"
_PREDICTIONS_TARGET = "predictions"

#: Default mapping of logical target names to their JSONL filenames.
#: Passed as the *targets* default in :func:`check_data_sufficiency`.
DEFAULT_DATA_SUFFICIENCY_TARGETS: dict[str, str] = {
    _GATE_ACTIVATIONS_TARGET: GATE_ACTIVATIONS_FILENAME,
    _PREDICTIONS_TARGET: PREDICTIONS_FILENAME,
    "usage": "usage.jsonl",
}


def check_data_sufficiency(
    data_dir: Path,
    targets: dict[str, str] | None = None,
    gate_activation_backend: GateActivationBackend | None = None,
    prediction_backend: PredictionBackend | None = None,
) -> list[str]:
    """Return target names whose stores have fewer than the minimum observations.

    For each entry in *targets* (defaults to
    :data:`DEFAULT_DATA_SUFFICIENCY_TARGETS`), counts the number of
    non-empty lines in the corresponding JSONL file under *data_dir*.
    Target names whose file is missing, unreadable, or contains fewer
    than :data:`DATA_SUFFICIENCY_MIN_OBSERVATIONS` records are included
    in the returned list.

    When *gate_activation_backend* is supplied, the
    :data:`_GATE_ACTIVATIONS_TARGET` target counts via
    ``len(backend.read_all())`` instead of a filename walk; likewise
    *prediction_backend* routes the :data:`_PREDICTIONS_TARGET` count — so each
    flag-selected (JSONL or ``state.db``) store is its target's source of truth.
    Any target without a supplied backend stays filename-counted.

    Returns an empty list when every target meets the threshold.
    Deterministic, zero SDK cost.
    """
    if targets is None:
        targets = DEFAULT_DATA_SUFFICIENCY_TARGETS
    insufficient: list[str] = []
    for name, filename in targets.items():
        if (
            name == _GATE_ACTIVATIONS_TARGET
            and gate_activation_backend is not None
        ):
            count = len(gate_activation_backend.read_all())
        elif (
            name == _PREDICTIONS_TARGET
            and prediction_backend is not None
        ):
            count = len(prediction_backend.read_all())
        else:
            path = data_dir / filename
            count = 0
            if path.exists():
                try:
                    with open(path, encoding="utf-8") as f:
                        for line in f:
                            if line.strip():
                                count += 1
                except OSError:
                    pass
        if count < DATA_SUFFICIENCY_MIN_OBSERVATIONS:
            insufficient.append(name)
    return insufficient


# Per-instrument line cap for :func:`format_data_sufficiency_context` output.
_DATA_SUFFICIENCY_LINE_CAP = 200


def format_data_sufficiency_context(
    data_dir: Path,
    targets: dict[str, str] | None = None,
    threshold: int = DATA_SUFFICIENCY_MIN_OBSERVATIONS,
    gate_activation_backend: GateActivationBackend | None = None,
    prediction_backend: PredictionBackend | None = None,
) -> str:
    """Build a per-instrument data-sufficiency context string.

    For each entry in *targets* (defaults to
    :data:`DEFAULT_DATA_SUFFICIENCY_TARGETS`), counts non-empty JSONL lines
    and reports ``MET``, ``UNMET``, or ``no data`` against *threshold*.

    When *gate_activation_backend* is supplied, the
    :data:`_GATE_ACTIVATIONS_TARGET` target counts via
    ``len(backend.read_all())`` (always an integer, so it reads ``MET`` /
    ``UNMET``, never ``no data``); likewise *prediction_backend* routes the
    :data:`_PREDICTIONS_TARGET` count. Any target without a supplied backend
    stays filename-counted.

    Each instrument line is capped at :data:`_DATA_SUFFICIENCY_LINE_CAP`
    characters.  Returns an empty string when *targets* is empty.
    Deterministic, zero SDK cost.
    """
    if targets is None:
        targets = DEFAULT_DATA_SUFFICIENCY_TARGETS
    if not targets:
        return ""

    lines: list[str] = ["Data sufficiency:"]
    for name, filename in sorted(targets.items()):
        count: int | None = None  # None means file missing/unreadable
        if (
            name == _GATE_ACTIVATIONS_TARGET
            and gate_activation_backend is not None
        ):
            count = len(gate_activation_backend.read_all())
        elif (
            name == _PREDICTIONS_TARGET
            and prediction_backend is not None
        ):
            count = len(prediction_backend.read_all())
        else:
            path = data_dir / filename
            if path.exists():
                try:
                    c = 0
                    with open(path, encoding="utf-8") as f:
                        for raw_line in f:
                            if raw_line.strip():
                                c += 1
                    count = c
                except OSError:
                    pass

        if count is None:
            entry = f"  - {name}: no data"
        else:
            status = "MET" if count >= threshold else "UNMET"
            entry = f"  - {name}: {count}/{threshold} records — {status}"

        if len(entry) > _DATA_SUFFICIENCY_LINE_CAP:
            entry = entry[: _DATA_SUFFICIENCY_LINE_CAP - 1] + "…"
        lines.append(entry)

    return "\n".join(lines)
