"""Frame-discharge contract — the sole owner binding a task's declared shape to
what it produced.

A self-directed task carries two shapes nothing else reconciles. The **frame** is
how the goal string describes the work — :func:`~maestro.audit_checks.classify_task_shape`
reads it from the goal alone. The **discharge** is what the completed
:class:`~maestro.memory.TaskRecord` actually produced: a kept git branch of tracked
changes (``construction``), or only ephemeral writes under the gitignored
``artifacts/`` directory (``investigation``). Nothing today compares the two, which
lets a task be framed as a loseless read-only investigation and then discharged as
construction — a fresh test file, a new module — because construction is a motion
that reliably "succeeds" while a genuine investigation has no defined, loseable
deliverable. Such a task collects a ``success`` verdict wearing an investigation
costume.

This module is the single, provenance-stamped computer of the frame↔discharge
relation. It reuses ``classify_task_shape`` for the frame side (no rival classifier
is born) and keys the discharge purely on the ``branch`` tracked-change watermark.
:func:`assess_frame_discharge` stamps ``COSTUME`` when an investigation frame was
discharged as construction, ``MATCH`` for an aligned pair (an ``artifacts/``-only
investigation, or an honest construction), and ``N/A`` for a non-shape goal.

Pure functions, zero SDK cost, no I/O. AST-enforced as the sole owner of the
discharge relation by ``tests/structural_guards.py``'s ``frame_discharge`` guard.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from maestro.audit_checks import classify_task_shape

if TYPE_CHECKING:
    from maestro.audit_checks import TaskShape
    from maestro.memory import TaskRecord

#: The two discharge categories, keyed purely on the ``branch`` tracked-change
#: watermark. Finer discrimination (a ``mutation`` net-diff category, or path-based
#: ``maestro/`` vs ``tests/`` vs ``docs/`` classification) is deferred — it needs the
#: changed-file list surfaced into ``TaskRecord``, which this increment does not add.
Discharge = Literal["investigation", "construction"]

#: The frame shapes that count as a read-only investigation for the costume test. A
#: single-member set today; kept a ``frozenset`` so a later increment can widen it
#: (e.g. to ``measurement``) without reshaping the predicate. A frame outside this
#: set is never a costume regardless of discharge.
_INVESTIGATION_FRAMES: frozenset[str] = frozenset({"investigation"})


@dataclass(frozen=True)
class FrameDischargeVerdict:
    """The frame↔discharge verdict plus the provenance it was computed from.

    ``verdict`` is ``COSTUME`` (an investigation frame discharged as construction),
    ``MATCH`` (an aligned pair — an ``artifacts/``-only investigation or an honest
    construction), or ``N/A`` (a non-shape goal — ``classify_task_shape`` returned
    ``other``). ``frame`` and ``discharge`` carry the two sides the verdict was
    derived from, so a caller (or a test) can see *why* the verdict was stamped
    without re-deriving it — mirroring the provenance-stamped verdict style used
    across the tracker.
    """

    verdict: Literal["COSTUME", "MATCH", "N/A"]
    frame: TaskShape
    discharge: Discharge


def classify_frame(goal: str) -> TaskShape:
    """Classify *goal*'s declared shape, delegating to the one goal-string classifier.

    The frame side of the contract reuses
    :func:`~maestro.audit_checks.classify_task_shape` verbatim so no rival shape
    classifier is born; this indirection is the single seam the sole-owner guard (and
    a later gate-fold) read the frame through.
    """
    return classify_task_shape(goal)


def is_investigation_frame(goal: str) -> bool:
    """Return ``True`` when *goal*'s frame is a read-only investigation shape.

    The single seam through which both the record-time verdict
    (:func:`assess_frame_discharge`) and the choice-phase task-generation gate
    ask "is this an investigation frame?", so the frame notion has exactly one
    home. Reuses :func:`classify_frame` and tests membership in
    :data:`_INVESTIGATION_FRAMES`, the same frozenset ``assess_frame_discharge``
    reads — a later increment can widen the set (e.g. to ``measurement``)
    without forking the predicate. Pure, deterministic, no I/O.
    """
    return classify_frame(goal) in _INVESTIGATION_FRAMES


def classify_discharge(record: TaskRecord) -> Discharge:
    """Classify what *record* actually produced from its ``branch`` watermark.

    ``record.branch is None`` means the run kept no branch — only ephemeral writes
    under the gitignored ``artifacts/`` directory reached disk — a read-only
    ``investigation`` discharge. A set ``branch`` is a kept branch of committed
    tracked files, necessarily outside ``artifacts/``: a ``construction`` discharge.
    Keys purely on the tracked-change watermark; no path inspection (deferred).
    """
    if record.branch is None:
        return "investigation"
    return "construction"


def assess_frame_discharge(record: TaskRecord) -> FrameDischargeVerdict:
    """Stamp the frame↔discharge verdict for a completed *record*.

    ``COSTUME`` when the frame is an investigation shape (:data:`_INVESTIGATION_FRAMES`)
    and the discharge is ``construction`` — a task framed as a loseless read-only
    investigation that quietly produced tracked files, the pattern this owner exists
    to catch. ``N/A`` when the goal names no shape (``classify_task_shape`` returns
    ``other``), so a non-shape goal is never judged. ``MATCH`` for every aligned pair
    — an ``artifacts/``-only investigation (guarding against a false positive on an
    honest clean investigation) or an honest construction. Pure, deterministic, no
    I/O.
    """
    frame = classify_frame(record.goal)
    discharge = classify_discharge(record)
    verdict: Literal["COSTUME", "MATCH", "N/A"]
    if frame == "other":
        verdict = "N/A"
    elif frame in _INVESTIGATION_FRAMES and discharge == "construction":
        verdict = "COSTUME"
    else:
        verdict = "MATCH"
    return FrameDischargeVerdict(verdict=verdict, frame=frame, discharge=discharge)


def measure_frame_discharge_costumes(records: list[TaskRecord]) -> list[str]:
    """Return the ``task_id``s in *records* whose frame↔discharge verdict is COSTUME.

    Runs the owner's :func:`assess_frame_discharge` over each record in window
    order and collects the ``task_id`` of every one stamped ``COSTUME`` — an
    investigation-framed goal discharged as tracked construction. Window order is
    preserved and ``task_id``s are deduplicated (a repeated id keeps its first
    position). Reads the owner verdict, never re-deriving the frame↔discharge
    relation, so the sole-owner guard stays green. Returns ``[]`` when no record
    is a costume — the empty case the display sensor self-extinguishes on. Pure
    function, zero SDK cost, no I/O — mirrors
    :func:`~maestro.audit_checks.measure_undefined_coinages`.
    """
    seen: set[str] = set()
    costume_ids: list[str] = []
    for record in records:
        if assess_frame_discharge(record).verdict != "COSTUME":
            continue
        if record.task_id in seen:
            continue
        seen.add(record.task_id)
        costume_ids.append(record.task_id)
    return costume_ids


def format_measurement_costume_summary(
    costume_task_ids: list[str],
    max_ids: int = 5,
) -> str:
    """Build a ``MEASUREMENT-COSTUME`` line naming the costume-discharged tasks.

    Names up to *max_ids* task ids (window order preserved by
    :func:`measure_frame_discharge_costumes`) whose investigation frame was
    discharged as tracked construction, so contemplation can see exactly which
    recent tasks wore an investigation costume. Returns ``""`` when
    *costume_task_ids* is empty or *max_ids* < 1, so the caller injects the line
    only when non-empty — self-extinguishing the moment no task is a costume,
    mirroring :func:`~maestro.audit_checks.format_undefined_coinage_summary` /
    :func:`~maestro.audit_checks.format_task_shape_summary`. Pure function, zero
    SDK cost.
    """
    if not costume_task_ids or max_ids < 1:
        return ""
    ids = ", ".join(costume_task_ids[:max_ids])
    return (
        f"MEASUREMENT-COSTUME: recent tasks framed as read-only investigation but "
        f"discharged as tracked construction: {ids}. An investigation has no "
        f"loseable deliverable — name what was actually built or reframe the goal."
    )
