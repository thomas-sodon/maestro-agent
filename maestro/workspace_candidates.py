"""Candidate adapters for the conscious-access workspace (proposal 130).

The non-leaf sibling of :mod:`maestro.conscious_workspace`. That module is a
deliberate leaf — standard library plus :mod:`maestro.fileutil` /
:mod:`maestro.secret_scan` only, so :mod:`maestro.state_store` can import it for
the SQLite adapter without a cycle. The adapters here read the existing
structured-observation owners (``DiagnosticSnapshot``, ``AuditResult``,
``TaskRecord``, ``SelfModel``, the ``wip_primacy.TaskFocus`` /
``pr_facts.LivePlan`` obligation projections, the loaded ``QueuedTask`` list, the
agent's plain in-process throttle booleans, and the scored reflection bets
``audit_checks_calibration.load_scored_reflection_bets`` yields), which those readers cannot
live in the leaf without importing back through ``state_store``. So the pure
scoring/selection core stays in the leaf and these owner-reading adapters land
here.

Two attend-phase seams sit beside the adapters rather than in the phase body:
:func:`load_prediction_verdict_inputs`, the one **read** this module performs (a
named seam over the local prediction ledger — no host, git or subprocess seam),
and :func:`declare_blind_spots`, the ``AttentionSchema.blind_spots`` vocabulary.

Every *adapter* is a **pure** function over one already-loaded record, returning a
``list[WorkspaceItem]`` built through the leaf's sole item constructor
:func:`~maestro.conscious_workspace.build_workspace_item` (which head-truncates
the summary and refuses any credential); a single credential-bearing candidate
is dropped rather than aborting the whole batch. Two permanent boundaries:

* **Provenance, not content, decides trust.** An item's :class:`TrustClass` is
  fixed by *which owner produced it* — ``authenticated_human`` only for the
  operator directive queues, never inferred from what a summary says.
* **Attention is not authorization.** These adapters name and rank; they call no
  gate, grant no capability, and reference no symbol from
  :mod:`maestro.task_gates` or :mod:`maestro.tool_gating`. A selected item can
  never become an authorization.

Records that carry their own observation timestamp (``AuditResult.timestamp``,
``TaskRecord.timestamp``, ``QueuedTask.last_touched_at``, and — for the
burn-rate throttle fact — the throttle's own entry time) use it; the
timestamp-less owners (``DiagnosticSnapshot``, ``SelfModel``, the ``TaskFocus`` /
``LivePlan`` obligation projections, and the maintenance-mode fact) take an
injected ``now`` so the adapter stays pure and reads no clock — the
``now``-injection seam :func:`maestro.deed_ledger.build_deed` documents.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence

from maestro.audit_checks_calibration import (
    PredictionBackend,
    load_scored_reflection_bets,
)
from maestro.conscious_workspace import (
    SourceKind,
    TrustClass,
    WorkspaceItem,
    build_workspace_item,
)
from maestro.consciousness import SelfModel
from maestro.diagnostics import DiagnosticSnapshot, is_rising_high_cost
from maestro.memory import AuditResult, TaskRecord
from maestro.pr_facts import LivePlan
from maestro.secret_scan import has_hard_block
from maestro.task_queue import QueuedTask
from maestro.wip_primacy import Discretionary, TaskFocus, TendOwnedPR

# ---------------------------------------------------------------------------
# Per-source confidence — how far the *owner* vouches for a fact of that kind.
# Fixed by provenance, never read from a summary's text.
# ---------------------------------------------------------------------------

#: Confidence stamped on a human-directive candidate — an operator directive is
#: taken at face value.
_CONFIDENCE_HUMAN = 1.0
#: Confidence stamped on a diagnostic candidate — a zero-SDK mechanical reading.
_CONFIDENCE_DIAGNOSTIC = 0.9
#: Confidence stamped on an audit-finding candidate — a deterministic rule hit.
_CONFIDENCE_AUDIT = 0.8
#: Confidence stamped on a task-outcome candidate — a recorded, verified result.
_CONFIDENCE_TASK = 0.85
#: Confidence stamped on a self-model candidate — the agent's own inference, the
#: least-trusted class.
_CONFIDENCE_SELF_MODEL = 0.5
#: Confidence stamped on a PR-focus candidate — a measured host/git obligation, a
#: zero-SDK mechanical reading like the diagnostic snapshot.
_CONFIDENCE_PR_FOCUS = 0.9
#: Confidence stamped on a queue-pressure candidate — an exact count of the
#: loaded queue, a mechanical reading.
_CONFIDENCE_QUEUE = 0.9
#: Confidence stamped on a budget-state candidate — a plain in-process throttle
#: boolean, a mechanical reading.
_CONFIDENCE_BUDGET = 0.9
#: Confidence stamped on a prediction-verdict candidate — a bet the calibration
#: oracle scored off the ledgers, so it is vouched for like a diagnostic reading
#: rather than like the model's own self-grade (which never becomes a candidate).
_CONFIDENCE_PREDICTION = 0.9


def _digest(text: str) -> str:
    """Return a short, stable hash of *text* for use as an opaque ``source_ref``.

    Human directives and self-model entries carry no owner-assigned id, so their
    ``source_ref`` — the workspace's dedup identity — is a content digest: two
    verbatim-identical directives share one identity and collapse.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _item_id(
    source_kind: SourceKind, source_ref: str, observed_at: str, summary: str,
) -> str:
    """Return a deterministic ``wi-<hash12>`` id for a candidate.

    Derived from the candidate's identifying fields so identical facts observed
    at the same instant yield the same id — the determinism the moment hashes
    depend on. Canonicalised the same way the leaf's moment hashes are, so the
    digest is stable across runs.
    """
    canonical = json.dumps(
        [source_kind, source_ref, observed_at, summary], separators=(",", ":"),
    )
    return f"wi-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:12]}"


def _candidate(
    *,
    source_kind: SourceKind,
    source_ref: str,
    trust: TrustClass,
    confidence: float,
    observed_at: str,
    summary: str,
    lineage: Sequence[str] = (),
) -> WorkspaceItem | None:
    """Build one :class:`WorkspaceItem`, or ``None`` when its summary is refused.

    A *summary* tripping :func:`maestro.secret_scan.has_hard_block` is dropped
    (returns ``None``) rather than propagating the :class:`ValueError`
    :func:`~maestro.conscious_workspace.build_workspace_item` raises, so a single
    credential-bearing directive, goal or question cannot abort the whole gather:
    its siblings are still adapted and the credential is still never persisted.
    The refusal predicate is the leaf's own, reused here — the check is an
    explicit provenance filter, not a swallowed exception.

    *lineage* (proposal 130) names the originating workspace item ids a recurrent
    candidate descends from; it defaults to empty and only the task-outcome
    adapter passes it, so an ordinary candidate is unchanged. The ``item_id`` is
    derived from the identifying fields (not lineage), so a recurrent candidate
    keeps the same id it would have carried without lineage.
    """
    if has_hard_block(summary):
        return None
    return build_workspace_item(
        item_id=_item_id(source_kind, source_ref, observed_at, summary),
        source_kind=source_kind,
        source_ref=source_ref,
        observed_at=observed_at,
        trust=trust,
        confidence=confidence,
        expires_at=None,
        summary=summary,
        lineage=lineage,
    )


def _nonempty(candidates: Iterable[WorkspaceItem | None]) -> list[WorkspaceItem]:
    """Drop the ``None`` placeholders a refused (credential-bearing) summary leaves."""
    return [candidate for candidate in candidates if candidate is not None]


def candidates_from_human_directives(
    self_model: SelfModel, *, now: str,
) -> list[WorkspaceItem]:
    """Adapt the operator directive queues into ``authenticated_human`` items.

    Covers ``SelfModel.pending_user_requests`` (plain directive strings) and
    ``pending_pr_requests`` (each carrying a ``directive`` and optional
    ``pr_branch``). The only source that mints the ``authenticated_human`` trust
    class — fixed by the queue the directive came from, never by its wording.
    """
    items: list[WorkspaceItem | None] = []
    for directive in self_model.pending_user_requests:
        items.append(
            _candidate(
                source_kind="human_directive",
                source_ref=_digest(directive),
                trust="authenticated_human",
                confidence=_CONFIDENCE_HUMAN,
                observed_at=now,
                summary=directive,
            ),
        )
    for request in self_model.pending_pr_requests:
        items.append(
            _candidate(
                source_kind="human_directive",
                source_ref=request.pr_branch or _digest(request.directive),
                trust="authenticated_human",
                confidence=_CONFIDENCE_HUMAN,
                observed_at=now,
                summary=request.directive,
            ),
        )
    return _nonempty(items)


def candidates_from_diagnostics(
    snapshot: DiagnosticSnapshot, *, now: str,
) -> list[WorkspaceItem]:
    """Adapt a ``DiagnosticSnapshot`` into ``mechanical_observation`` items.

    One candidate per operationally-relevant fact the snapshot already carries:
    recent errors, a quota warning, a rate-limit signal, consecutive failures,
    and a noteworthy cost trend. The cost fact reuses the canonical
    :func:`maestro.diagnostics.is_rising_high_cost` predicate (plus the transient
    ``"spiking"`` trend) rather than re-deriving a cost threshold here. The
    snapshot carries no timestamp of its own, so *now* is its observation time.
    """
    items: list[WorkspaceItem | None] = []
    if snapshot.recent_error_count > 0:
        items.append(
            _candidate(
                source_kind="diagnostic_snapshot",
                source_ref="recent_errors",
                trust="mechanical_observation",
                confidence=_CONFIDENCE_DIAGNOSTIC,
                observed_at=now,
                summary=f"{snapshot.recent_error_count} recent ERROR-level log entries",
            ),
        )
    if snapshot.has_quota_warning:
        items.append(
            _candidate(
                source_kind="diagnostic_snapshot",
                source_ref="quota_warning",
                trust="mechanical_observation",
                confidence=_CONFIDENCE_DIAGNOSTIC,
                observed_at=now,
                summary="API quota warning present in the recent log window",
            ),
        )
    if snapshot.has_rate_limit:
        items.append(
            _candidate(
                source_kind="diagnostic_snapshot",
                source_ref="rate_limit",
                trust="mechanical_observation",
                confidence=_CONFIDENCE_DIAGNOSTIC,
                observed_at=now,
                summary="Rate-limit signal present in the recent log window",
            ),
        )
    if snapshot.consecutive_failures > 0:
        items.append(
            _candidate(
                source_kind="diagnostic_snapshot",
                source_ref="consecutive_failures",
                trust="mechanical_observation",
                confidence=_CONFIDENCE_DIAGNOSTIC,
                observed_at=now,
                summary=f"{snapshot.consecutive_failures} consecutive iteration failures",
            ),
        )
    if snapshot.cost_trend == "spiking" or is_rising_high_cost(snapshot):
        items.append(
            _candidate(
                source_kind="diagnostic_snapshot",
                source_ref="cost_trend",
                trust="mechanical_observation",
                confidence=_CONFIDENCE_DIAGNOSTIC,
                observed_at=now,
                summary=f"Cost trend is {snapshot.cost_trend}",
            ),
        )
    return _nonempty(items)


def candidates_from_audit_findings(
    audit_result: AuditResult,
) -> list[WorkspaceItem]:
    """Adapt an ``AuditResult``'s findings into ``mechanical_observation`` items.

    One candidate per :class:`~maestro.audit_checks.AuditFinding`, keyed on the
    finding's ``rule`` (a stable string) as its ``source_ref`` and carrying the
    finding's deterministic ``statement`` as the summary. Uses the result's own
    ``timestamp`` as the observation time.
    """
    return _nonempty(
        _candidate(
            source_kind="audit_finding",
            source_ref=finding.rule,
            trust="mechanical_observation",
            confidence=_CONFIDENCE_AUDIT,
            observed_at=audit_result.timestamp,
            summary=finding.statement,
        )
        for finding in audit_result.findings
    )


def candidates_from_task_outcomes(
    records: Sequence[TaskRecord],
) -> list[WorkspaceItem]:
    """Adapt recent ``TaskRecord``s into ``mechanical_observation`` items.

    One candidate per record, keyed on ``task_id`` and carrying the record's
    ``outcome`` and ``goal`` as the summary. Each record's own ``timestamp`` is
    the observation time.

    A record that was itself steered by a prior conscious moment carries the
    admitted item ids it cited (``TaskRecord.workspace_item_ids``, stamped by the
    task-generation producer); this adapter threads that citation through as the
    candidate's ``lineage`` (proposal 130), closing the recurrence loop — the
    successor candidate names the earlier items whose decision produced it. A
    record with no citation (external task, legacy row, ``None``) yields a
    lineage-less candidate exactly as before. Because lineage is attached only in
    this one *mechanical*-observation adapter, an ``agent_derived`` self-model item
    can never acquire it.
    """
    return _nonempty(
        _candidate(
            source_kind="task_outcome",
            source_ref=record.task_id,
            trust="mechanical_observation",
            confidence=_CONFIDENCE_TASK,
            observed_at=record.timestamp,
            summary=f"Task {record.outcome}: {record.goal}",
            lineage=tuple(record.workspace_item_ids or ()),
        )
        for record in records
    )


def candidates_from_self_model(
    self_model: SelfModel, *, now: str,
) -> list[WorkspaceItem]:
    """Adapt the self-model's open questions, limitations and directive.

    Produces ``agent_derived`` items — the model's own inference, the
    least-trusted class — over ``open_questions``, ``limitations_known`` and a
    set ``next_task_directive``. The self-model carries no per-entry timestamp,
    so *now* is the observation time.
    """
    items: list[WorkspaceItem | None] = []
    for question in self_model.open_questions:
        items.append(
            _candidate(
                source_kind="self_model",
                source_ref=f"question:{_digest(question)}",
                trust="agent_derived",
                confidence=_CONFIDENCE_SELF_MODEL,
                observed_at=now,
                summary=question,
            ),
        )
    for limitation in self_model.limitations_known:
        items.append(
            _candidate(
                source_kind="self_model",
                source_ref=f"limitation:{_digest(limitation)}",
                trust="agent_derived",
                confidence=_CONFIDENCE_SELF_MODEL,
                observed_at=now,
                summary=limitation,
            ),
        )
    if self_model.next_task_directive:
        items.append(
            _candidate(
                source_kind="self_model",
                source_ref="next_task_directive",
                trust="agent_derived",
                confidence=_CONFIDENCE_SELF_MODEL,
                observed_at=now,
                summary=self_model.next_task_directive,
            ),
        )
    return _nonempty(items)


def candidates_from_pr_focus(
    focus: TaskFocus, live_plans: Sequence[LivePlan], *, now: str,
) -> list[WorkspaceItem]:
    """Adapt the loop's owned-WIP obligation into ``mechanical_observation`` items.

    Emits one candidate for a :class:`~maestro.wip_primacy.TendOwnedPR` verdict —
    the loop owes work on a named PR with a self-clearable ``owned_blocker``, the
    most directly actionable fact it holds — keyed on the PR number
    (``source_ref=f"pr:{focus.pr_number}"``). A
    :class:`~maestro.wip_primacy.Discretionary` verdict contributes nothing: the
    absence of owned WIP is not a fact worth a slot.

    Emits one further candidate per :class:`~maestro.pr_facts.LivePlan` that has an
    open PR (``pr_number is not None``), keyed on the plan id
    (``source_ref=f"plan:{plan_id}"``). ``LivePlan.rationale`` is deliberately
    never read into a summary — it is LLM-authored prose, and self-authored
    narration must not enter the workspace as a mechanical observation. Neither the
    verdict nor a plan carries a timestamp, so *now* is the observation time.
    """
    items: list[WorkspaceItem | None] = []
    if isinstance(focus, TendOwnedPR):
        items.append(
            _candidate(
                source_kind="pr_focus",
                source_ref=f"pr:{focus.pr_number}",
                trust="mechanical_observation",
                confidence=_CONFIDENCE_PR_FOCUS,
                observed_at=now,
                summary=(
                    f"Owe work on PR #{focus.pr_number}: {focus.owned_blocker}"
                ),
            ),
        )
    for plan in live_plans:
        if plan.pr_number is None:
            continue
        items.append(
            _candidate(
                source_kind="pr_focus",
                source_ref=f"plan:{plan.plan_id}",
                trust="mechanical_observation",
                confidence=_CONFIDENCE_PR_FOCUS,
                observed_at=now,
                summary=(
                    f"Live plan ({plan.status}) '{plan.title}' → PR "
                    f"#{plan.pr_number}"
                ),
            ),
        )
    return _nonempty(items)


def candidates_from_budget_state(
    *,
    burn_rate_throttle_active: bool,
    maintenance_mode_active: bool,
    throttle_entered_at: str | None,
    now: str,
) -> list[WorkspaceItem]:
    """Adapt the throttle constraints into ``mechanical_observation`` items.

    Takes the two constituent booleans rather than the union
    ``budget_throttle_active`` so the item names *which* constraint is in force.
    Emits one candidate for an active burn-rate throttle
    (``source_ref="burn_rate_throttle"``), observed at *throttle_entered_at* or,
    absent one, *now* — so the throttle's own entry time is the honest observation
    instant — and one for active maintenance mode
    (``source_ref="maintenance_mode"``, observed at *now*, which carries no entry
    time). Deliberately takes no ``CostRateSnapshot``: the burn-rate figure would
    require reading ``cost_samples.jsonl``, while the throttle state is the
    operationally relevant fact and is a plain in-process value.
    """
    items: list[WorkspaceItem | None] = []
    if burn_rate_throttle_active:
        items.append(
            _candidate(
                source_kind="budget_state",
                source_ref="burn_rate_throttle",
                trust="mechanical_observation",
                confidence=_CONFIDENCE_BUDGET,
                observed_at=throttle_entered_at or now,
                summary="Burn-rate throttle active — the LLM budget is constrained",
            ),
        )
    if maintenance_mode_active:
        items.append(
            _candidate(
                source_kind="budget_state",
                source_ref="maintenance_mode",
                trust="mechanical_observation",
                confidence=_CONFIDENCE_BUDGET,
                observed_at=now,
                summary="Maintenance mode active — the loop is operator-throttled",
            ),
        )
    return _nonempty(items)


def candidates_from_queue_pressure(
    queue: Sequence[QueuedTask], *, cap: int, now: str,
) -> list[WorkspaceItem]:
    """Adapt task-queue pressure into ``mechanical_observation`` items.

    Emits a depth fact when the queue is non-empty (``source_ref="depth"``),
    reporting filled slots against *cap* — the same ``free_slots = max(0, cap -
    len(queue))`` arithmetic :mod:`maestro.loop.task_generation` performs against
    ``Config.task_queue_max`` — and a head fact for the FIFO head
    (``source_ref=f"head:{task_id}"``) carrying the head's ``goal``, observed at
    the task's own ``last_touched_at`` since :class:`~maestro.task_queue.QueuedTask`
    carries its timestamp. An empty queue contributes nothing.
    """
    if not queue:
        return []
    free_slots = max(0, cap - len(queue))
    head = queue[0]
    items: list[WorkspaceItem | None] = [
        _candidate(
            source_kind="queue_pressure",
            source_ref="depth",
            trust="mechanical_observation",
            confidence=_CONFIDENCE_QUEUE,
            observed_at=now,
            summary=(
                f"{len(queue)} of {cap} task-queue slots filled "
                f"({free_slots} free)"
            ),
        ),
        _candidate(
            source_kind="queue_pressure",
            source_ref=f"head:{head.task_id}",
            trust="mechanical_observation",
            confidence=_CONFIDENCE_QUEUE,
            observed_at=head.last_touched_at,
            summary=head.goal,
        ),
    ]
    return _nonempty(items)


def _admits_prediction_bet(bet: Mapping[str, str]) -> bool:
    """Whether a scored bet becomes a ``prediction_verdict`` candidate.

    The single admission test shared by :func:`candidates_from_prediction_verdicts`
    and :func:`declare_blind_spots`: the verdict must be the calibration oracle's
    (``verdict_source == "measured"`` — authorship is the trust warrant) and the
    bet must carry the ``prediction_id`` that serves as its dedup identity. Both
    readers key on this one predicate so the declared measurement gap cannot
    disagree with what the adapter actually contributes.
    """
    return bet.get("verdict_source") == "measured" and bool(
        bet.get("prediction_id"),
    )


def declare_blind_spots(
    *,
    snapshot: DiagnosticSnapshot | None,
    audit_result: AuditResult | None,
    scored_bets: Sequence[Mapping[str, str]],
) -> list[str]:
    """Return the observe-only pass's measurement gaps, in a fixed order.

    The ``blind_spots`` vocabulary the attend phase stamps onto its moment's
    :class:`~maestro.conscious_workspace.AttentionSchema`: an honest self-model of
    what this pass did *not* sample rather than a silent omission. The owned-WIP
    focus is always declared (deriving it costs a ``gh pr list``, and the phase
    reaches no host/git/subprocess seam); the diagnostic snapshot, the audit
    result and the scored-bet ledger are each declared only when that owner had
    nothing to contribute. The prediction owner declares *two* distinct gaps,
    because :func:`candidates_from_prediction_verdicts` admits only a bet stamped
    ``verdict_source == "measured"`` *and* carrying a ``prediction_id``: an empty
    ledger is one gap, and a ledger whose every scored bet fails that admission
    test is another — without the second, such a ledger would withdraw the gap
    while contributing no candidate, under-declaring a real measurement gap. The
    second gap keys on the adapter's own predicate
    (:func:`_admits_prediction_bet`), so the declaration cannot drift from what
    the adapter actually admits. Lives here, beside the adapters whose owners it
    names, rather than in the phase body.
    """
    blind_spots = ["task_focus omitted: owned-WIP verdict needs a host call"]
    if snapshot is None:
        blind_spots.append("diagnostic snapshot absent (cold mid-iteration resume)")
    if audit_result is None:
        blind_spots.append("no audit result recorded yet")
    if not scored_bets:
        blind_spots.append("no scored reflection bets in the ledger yet")
    elif not any(_admits_prediction_bet(bet) for bet in scored_bets):
        blind_spots.append(
            "no measured reflection verdicts yet "
            "(no scored bet carries an identified, oracle-authored verdict)",
        )
    return blind_spots


def load_prediction_verdict_inputs(
    backend: PredictionBackend,
) -> list[dict[str, str]]:
    """Read the ninth owner's input — the scored reflection bets — for a caller.

    A thin named seam over :func:`maestro.audit_checks_calibration.load_scored_reflection_bets`
    so the attend phase names the owner rather than the ledger reader, and so the
    read stays beside the adapter that consumes it. A local ledger read (JSONL or
    SQLite): no host, git or subprocess seam.
    """
    return load_scored_reflection_bets(backend)


def candidates_from_prediction_verdicts(
    scored_bets: Sequence[Mapping[str, str]], *, now: str,
) -> list[WorkspaceItem]:
    """Adapt *measured* reflection verdicts into ``mechanical_observation`` items.

    Takes the scored-bet dicts
    :func:`maestro.audit_checks_calibration.load_scored_reflection_bets` yields and emits one
    candidate per bet whose ``verdict_source`` is ``"measured"`` — the calibration
    oracle authored that verdict by reading the ledgers, which is what makes it a
    mechanical observation. A bet stamped ``"narrated"`` (or carrying no stamp at
    all) is the model's own self-grade and is **skipped entirely**: authorship is
    the trust warrant, and a narrated grade never enters the workspace under a
    mechanical trust class. The bet's ``prediction_id`` is the ``source_ref``, so
    re-observing the same scored bet across iterations collapses to one dedup
    identity, and its own ``timestamp`` is the observation instant (falling back
    to *now* for a row that carries none). A credential-bearing summary is dropped
    by :func:`_candidate`, not raised.
    """
    items: list[WorkspaceItem | None] = []
    for bet in scored_bets:
        if not _admits_prediction_bet(bet):
            continue
        prediction_id = bet["prediction_id"]
        verdict = bet.get("verdict", "")
        prediction = bet.get("prediction", "")
        items.append(
            _candidate(
                source_kind="prediction_verdict",
                source_ref=prediction_id,
                trust="mechanical_observation",
                confidence=_CONFIDENCE_PREDICTION,
                observed_at=bet.get("timestamp") or now,
                summary=(
                    f"Scored self-prediction ({verdict}): {prediction}"
                ),
            ),
        )
    return _nonempty(items)


def gather_candidates(
    *,
    self_model: SelfModel,
    snapshot: DiagnosticSnapshot | None,
    audit_result: AuditResult | None,
    task_records: Sequence[TaskRecord],
    now: str,
    task_focus: TaskFocus | None = None,
    live_plans: Sequence[LivePlan] = (),
    queued_tasks: Sequence[QueuedTask] = (),
    queue_cap: int = 0,
    burn_rate_throttle_active: bool = False,
    maintenance_mode_active: bool = False,
    throttle_entered_at: str | None = None,
    scored_bets: Sequence[Mapping[str, str]] = (),
) -> list[WorkspaceItem]:
    """Concatenate every adapter's candidates in one fixed order.

    The order — human directives, diagnostics, audit findings, task outcomes,
    self-model, PR focus, queue pressure, budget state, prediction verdicts (nine
    owners) — is fixed so
    :func:`maestro.conscious_workspace.assemble_moment`, which folds the candidate
    identities into a moment's id in the order it receives them, is deterministic.
    A caller supplying only the five original inputs gets exactly the previous
    ordering: the three new owners each default to an empty contribution
    (*task_focus* ``None`` is treated as a :class:`~maestro.wip_primacy.Discretionary`
    verdict, *live_plans* / *queued_tasks* empty, the throttle booleans ``False``),
    and a caller that omits *scored_bets* likewise reproduces the pre-prediction
    ordering byte-for-byte — the ninth owner is appended last and contributes
    nothing when unset.

    *snapshot* and *audit_result* are optional: a caller without a fresh reading
    passes ``None`` and that owner contributes nothing this moment. A single
    credential-bearing candidate is dropped by its adapter (see
    :func:`_candidate`) rather than aborting the whole gather.
    """
    items = candidates_from_human_directives(self_model, now=now)
    if snapshot is not None:
        items.extend(candidates_from_diagnostics(snapshot, now=now))
    if audit_result is not None:
        items.extend(candidates_from_audit_findings(audit_result))
    items.extend(candidates_from_task_outcomes(task_records))
    items.extend(candidates_from_self_model(self_model, now=now))
    items.extend(
        candidates_from_pr_focus(
            task_focus if task_focus is not None else Discretionary(),
            live_plans,
            now=now,
        ),
    )
    items.extend(candidates_from_queue_pressure(queued_tasks, cap=queue_cap, now=now))
    items.extend(
        candidates_from_budget_state(
            burn_rate_throttle_active=burn_rate_throttle_active,
            maintenance_mode_active=maintenance_mode_active,
            throttle_entered_at=throttle_entered_at,
            now=now,
        ),
    )
    items.extend(candidates_from_prediction_verdicts(scored_bets, now=now))
    return items
