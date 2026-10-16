"""Offline evidence reader over the conscious-episode ledger (proposal 130).

Turns accumulated :class:`~maestro.conscious_workspace.ConsciousEpisode` rows
into six defensible, *structural* readings — limited-capacity moment selection,
receipted broadcast, recurrence, attention-schema agreement, temporal
continuity, and causal reach — that ground six of the global-workspace indicators
the ``consciousness-report`` CLI otherwise left as ``not_implemented``
placeholders.
Each indicator is scored independently, from mechanical facts only, into one
:data:`WorkspaceVerdict`:

* ``not_observed`` — no episodes to read.
* ``architectural`` — episodes exist but the behaviour never fired: no recorded
  competition (nothing was ever excluded) for limited access, or no broadcast
  receipt for receipted broadcast. The machinery is present but unexercised.
* ``behaviorally_supported`` — the behaviour fired within its bounds. Causal
  reach deliberately cannot reach this bar: it reads *declared* decision uptake,
  which the indicator matrix does not accept as behavioural support.
* ``refuted`` — a cap or provenance-integrity violation contradicts the claim.

One ledger fact shapes every evaluator: a single moment can leave **two** rows —
the ``attend`` phase appends a bootstrap row (empty receipts) and
``MaestroAgent._finalize_conscious_episode`` later appends a superseding row
sharing the same ``moment_id`` — so each evaluator runs
:func:`collapse_episodes_by_moment` first (last-recorded wins), folding *moments*
rather than rows, or every moment would be double-counted and its bootstrap row
would read as "never broadcast".

There is deliberately no total consciousness score, and no reading depends on
any free-text field: eloquent self-report cannot raise a reading (this module
reads no prose), and corrupt provenance *refutes* the affected indicator. A
leaf module — it imports only from :mod:`maestro.conscious_workspace` (the record
shapes, the authorized-consumer registry, and the two hash functions), so it
introduces no import cycle. Pure: no I/O, no network, no gate.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from maestro.conscious_workspace import (
    CONSCIOUS_CONSUMERS,
    ConsciousEpisode,
    compute_candidate_hash,
    compute_selection_hash,
)

#: The independent per-indicator verdict. Each global-workspace indicator is
#: scored to exactly one of these from structural facts; there is no aggregate
#: consciousness score. ``tests/test_consciousness_eval.py`` pins the closed set.
WorkspaceVerdict = Literal[
    "not_observed", "architectural", "behaviorally_supported", "refuted",
]


@dataclass(frozen=True)
class LimitedAccessReading:
    """The limited-capacity moment-selection reading over an episode tail.

    Every field is a mechanical count folded from the episodes' moments — no
    free-text is read — mirroring the frozen counts-bearing shape of
    :class:`~maestro.audit_checks_calibration.PremiseReport`.

    *episodes* — total episodes folded (the denominator).
    *competition_episodes* — episodes whose moment recorded at least one
    :class:`~maestro.conscious_workspace.ExcludedCandidate` (real competition for
    the bounded slots, not a dump).
    *admitted* — total admitted items across every moment.
    *excluded* — total excluded candidates across every moment.
    *violations* — cap or provenance-integrity breaches: a moment over its
    ``max_items`` (by ``schema.capacity_used`` or admitted count), over its
    ``max_chars`` (summed admitted-summary length), or carrying a
    ``selection_hash`` / ``candidate_hash`` that disagrees with a recomputation.
    *verdict* — the scored :data:`WorkspaceVerdict`.
    """

    episodes: int
    competition_episodes: int
    admitted: int
    excluded: int
    violations: int
    verdict: WorkspaceVerdict


@dataclass(frozen=True)
class ReceiptedBroadcastReading:
    """The receipted-broadcast reading over an episode tail.

    Folds each episode's delivery receipts into mechanical counts — again reading
    no prose — mirroring :class:`~maestro.audit_checks_calibration.PremiseReport`.

    *episodes* — total episodes folded (the denominator).
    *receipted_episodes* — episodes carrying at least one receipt (broadcast
    actually fired).
    *receipts* — total delivery receipts across every episode.
    *consumers_covered* — distinct *authorized* consumers (members of
    :data:`~maestro.conscious_workspace.CONSCIOUS_CONSUMERS`) named by a receipt.
    *violations* — integrity breaches: a receipt naming a consumer outside the
    authorized registry, whose ``moment_hash`` disagrees with its moment's stored
    ``selection_hash``, or whose ``moment_id`` disagrees with its moment's.
    *verdict* — the scored :data:`WorkspaceVerdict`.
    """

    episodes: int
    receipted_episodes: int
    receipts: int
    consumers_covered: int
    violations: int
    verdict: WorkspaceVerdict


@dataclass(frozen=True)
class RecurrenceReading:
    """The recurrence-loop reading over an episode tail.

    Recurrence fires when a *verified* action outcome re-enters a later moment as
    a lineage-bearing successor input — the
    :attr:`~maestro.conscious_workspace.WorkspaceItem.lineage` a ``task_outcome``
    candidate carries back, naming the admitted items whose decision produced it.
    Self-authored narration must not perpetuate itself, so a lineage-bearing item
    sourced anywhere but a mechanical task outcome is a provenance breach. Every
    field is a mechanical count folded from the moments — no prose is read —
    mirroring :class:`LimitedAccessReading`.

    *episodes* — total moments folded (the denominator).
    *recurrence_episodes* — moments carrying at least one lineage-bearing item
    (recurrence actually fired that moment).
    *lineage_items* — total lineage-bearing items across every moment.
    *violations* — lineage-bearing items whose ``source_kind`` is not
    ``task_outcome`` or whose ``trust`` is not ``mechanical_observation``: a
    self-authored item must never carry lineage.
    *verdict* — the scored :data:`WorkspaceVerdict`.
    """

    episodes: int
    recurrence_episodes: int
    lineage_items: int
    violations: int
    verdict: WorkspaceVerdict


@dataclass(frozen=True)
class SchemaAgreementReading:
    """The attention-schema-agreement reading over an episode tail.

    Checks each moment's :class:`~maestro.conscious_workspace.AttentionSchema`
    self-report against the actual selection trace: the schema's claimed load and
    omission must agree with what the moment really admitted and excluded, or the
    system's model of its own focus is not grounded. Reads only counts, no prose —
    mirroring :class:`LimitedAccessReading`.

    *episodes* — total moments folded (the denominator).
    *agreeing_episodes* — moments whose schema agreed with the selection trace on
    every check.
    *violations* — per-check disagreements: ``capacity_used`` unequal to the
    admitted count, ``omitted_count`` below the recorded-exclusion count (the
    exclusion trace is a capped subset, so ``>=`` is required, never ``==``), or
    ``capacity_used`` over the moment's ``max_items``.
    *verdict* — the scored :data:`WorkspaceVerdict`.
    """

    episodes: int
    agreeing_episodes: int
    violations: int
    verdict: WorkspaceVerdict


@dataclass(frozen=True)
class TemporalContinuityReading:
    """The temporal-continuity reading over an episode tail.

    Checks that the episode chain honestly names a predecessor or a typed gap
    across restarts: a ``genesis`` moment must not name a predecessor, and a
    non-genesis moment (``linked`` / ``crash_gap`` / ``disabled_interval``) must
    name one. Reads only the continuity markers, no prose — mirroring
    :class:`LimitedAccessReading`.

    *episodes* — total moments folded (the denominator).
    *linked_episodes* — moments with ``linked`` continuity (an ordinary
    successor).
    *gap_episodes* — moments with a typed gap continuity (``crash_gap`` /
    ``disabled_interval``).
    *genesis_episodes* — moments with ``genesis`` continuity (no predecessor).
    *violations* — a ``genesis`` moment carrying a ``predecessor_id``, or a
    non-genesis moment whose ``predecessor_id`` is ``None``.
    *verdict* — the scored :data:`WorkspaceVerdict`.
    """

    episodes: int
    linked_episodes: int
    gap_episodes: int
    genesis_episodes: int
    violations: int
    verdict: WorkspaceVerdict


@dataclass(frozen=True)
class CausalReachReading:
    """The causal-reach reading over an episode tail.

    Reads the decision-provenance fields a finalized episode carries —
    ``cited_task_ids`` (stamped by the task-generation decision) and
    ``cited_plan_ids`` (stamped by the plan-proposal decision) — against the
    delivery receipts proving the citing consumer actually received the moment.
    Counts only, no prose, mirroring :class:`LimitedAccessReading`.

    *episodes* — total moments folded (the denominator).
    *reaching_episodes* — moments carrying at least one *receipt-backed* citation
    of either kind. A moment whose only citation is unbacked earns no reach
    credit (it scores a violation instead), so the numerator and the violation
    count can never credit and indict the same citation at once.
    *cited_tasks* — total task ids cited across all moments, backed or not.
    *cited_plans* — total plan ids cited across all moments, backed or not.
    *violations* — a moment citing plans without an ``evolution`` receipt, or
    citing tasks without a ``task_generation`` receipt: a citation no delivery
    backs earns no credit.
    *verdict* — the scored :data:`WorkspaceVerdict`, capped at ``architectural``
    (see :func:`evaluate_causal_reach`).
    """

    episodes: int
    reaching_episodes: int
    cited_tasks: int
    cited_plans: int
    violations: int
    verdict: WorkspaceVerdict


def collapse_episodes_by_moment(
    episodes: Sequence[ConsciousEpisode],
) -> list[ConsciousEpisode]:
    """Collapse rows sharing a ``moment.moment_id``, last-recorded winning.

    One moment can leave **two** ledger rows: the ``attend`` phase appends a
    bootstrap row (empty receipts, empty citations) and
    ``MaestroAgent._finalize_conscious_episode`` later appends a superseding row
    sharing the same ``moment_id`` that carries the iteration's receipts and
    cited ids. Both evaluators must fold *moments*, not rows, or every moment is
    double-counted and its bootstrap row reads as "never broadcast". A pure
    last-wins ``{moment_id: episode}`` fold walked in the oldest-first
    ``read_all()`` order — dict insertion order keeps each moment at its
    first-seen position while a later row overwrites its value — so the survivors
    are returned in first-seen moment order carrying the last-recorded row.
    Mirrors the last-wins ``{id: record}`` fold in
    :func:`maestro.cost_tracking.aggregate_task_quality`.
    """
    collapsed: dict[str, ConsciousEpisode] = {}
    for episode in episodes:
        collapsed[episode.moment.moment_id] = episode
    return list(collapsed.values())


def evaluate_limited_access(
    episodes: Sequence[ConsciousEpisode],
) -> LimitedAccessReading:
    """Score limited-capacity moment selection from the episodes' selection traces.

    Collapses rows to moments (:func:`collapse_episodes_by_moment`) first, then
    folds each moment: counts admitted-vs-excluded, the moments exhibiting real
    competition, and every cap/hash violation. Verdict rules — no moments →
    ``not_observed``; any cap or hash violation → ``refuted`` (corrupt provenance
    cannot read as support); moments but competition never observed →
    ``architectural`` (bounded machinery present, but never a genuine selection);
    otherwise ``behaviorally_supported``. Pure; reads only structural fields.
    """
    moments = collapse_episodes_by_moment(episodes)
    competition_episodes = 0
    admitted = 0
    excluded = 0
    violations = 0
    for episode in moments:
        moment = episode.moment
        admitted += len(moment.items)
        excluded += len(moment.excluded)
        if moment.excluded:
            competition_episodes += 1
        if moment.schema.capacity_used > moment.max_items:
            violations += 1
        if len(moment.items) > moment.max_items:
            violations += 1
        if sum(len(item.summary) for item in moment.items) > moment.max_chars:
            violations += 1
        if moment.selection_hash != compute_selection_hash(
            moment.items, moment.schema,
        ):
            violations += 1
        if moment.candidate_hash != compute_candidate_hash(
            moment.items, moment.excluded,
        ):
            violations += 1

    verdict = _score(
        episodes=len(moments),
        violations=violations,
        fired=competition_episodes > 0,
    )
    return LimitedAccessReading(
        episodes=len(moments),
        competition_episodes=competition_episodes,
        admitted=admitted,
        excluded=excluded,
        violations=violations,
        verdict=verdict,
    )


def evaluate_receipted_broadcast(
    episodes: Sequence[ConsciousEpisode],
) -> ReceiptedBroadcastReading:
    """Score receipted broadcast from the episodes' delivery receipts.

    Collapses rows to moments (:func:`collapse_episodes_by_moment`) first, then
    folds each moment's ``receipts``: counts the moments that carried any receipt,
    the total receipts, the distinct authorized consumers covered, and every
    integrity violation — a receipt naming a consumer outside
    :data:`~maestro.conscious_workspace.CONSCIOUS_CONSUMERS`, a ``moment_hash``
    disagreeing with the moment's stored ``selection_hash``, or a ``moment_id``
    disagreeing with its moment's. Verdict rules — no moments → ``not_observed``;
    any integrity violation → ``refuted``; moments but zero receipts
    (observation-only, broadcast never fired) → ``architectural``; otherwise
    ``behaviorally_supported``. Pure; reads only structural fields.
    """
    moments = collapse_episodes_by_moment(episodes)
    receipted_episodes = 0
    receipts = 0
    violations = 0
    covered: set[str] = set()
    for episode in moments:
        if episode.receipts:
            receipted_episodes += 1
        for receipt in episode.receipts:
            receipts += 1
            consumer_authorized = receipt.consumer in CONSCIOUS_CONSUMERS
            if not consumer_authorized:
                violations += 1
            if receipt.moment_hash != episode.moment.selection_hash:
                violations += 1
            if receipt.moment_id != episode.moment.moment_id:
                violations += 1
            if consumer_authorized:
                covered.add(receipt.consumer)

    verdict = _score(
        episodes=len(moments),
        violations=violations,
        fired=receipts > 0,
    )
    return ReceiptedBroadcastReading(
        episodes=len(moments),
        receipted_episodes=receipted_episodes,
        receipts=receipts,
        consumers_covered=len(covered),
        violations=violations,
        verdict=verdict,
    )


def evaluate_recurrence(
    episodes: Sequence[ConsciousEpisode],
) -> RecurrenceReading:
    """Score the recurrence loop from the moments' lineage-bearing items.

    Collapses rows to moments (:func:`collapse_episodes_by_moment`) first, then
    folds each moment's items: counts the lineage-bearing items (recurrence
    fired) and flags a provenance violation for any lineage-bearing item not
    sourced from a mechanical task outcome (``source_kind == "task_outcome"`` and
    ``trust == "mechanical_observation"``) — self-authored narration must never
    perpetuate itself. Verdict rules — no moments → ``not_observed``; any
    provenance violation → ``refuted``; moments but no lineage yet →
    ``architectural`` (the recurrence machinery is present but never fired);
    otherwise ``behaviorally_supported``. Pure; reads only structural fields.
    """
    moments = collapse_episodes_by_moment(episodes)
    recurrence_episodes = 0
    lineage_items = 0
    violations = 0
    for episode in moments:
        moment_lineage_items = 0
        for item in episode.moment.items:
            if not item.lineage:
                continue
            moment_lineage_items += 1
            lineage_items += 1
            if (
                item.source_kind != "task_outcome"
                or item.trust != "mechanical_observation"
            ):
                violations += 1
        if moment_lineage_items:
            recurrence_episodes += 1

    verdict = _score(
        episodes=len(moments),
        violations=violations,
        fired=lineage_items > 0,
    )
    return RecurrenceReading(
        episodes=len(moments),
        recurrence_episodes=recurrence_episodes,
        lineage_items=lineage_items,
        violations=violations,
        verdict=verdict,
    )


def evaluate_schema_agreement(
    episodes: Sequence[ConsciousEpisode],
) -> SchemaAgreementReading:
    """Score attention-schema agreement from each moment's schema vs. its trace.

    Collapses rows to moments (:func:`collapse_episodes_by_moment`) first, then
    folds each moment: a moment *agrees* when its schema's ``capacity_used``
    equals the admitted count, its ``omitted_count`` is at least the recorded
    exclusion count (the trace is a capped subset, so ``>=`` not ``==``), and its
    ``capacity_used`` does not exceed the moment's ``max_items``; every failing
    check is one violation. Verdict rules — no moments → ``not_observed``; any
    violation → ``refuted``; otherwise ``behaviorally_supported``. Every moment
    carries a checkable schema, so this indicator has no unexercised state and
    never reads ``architectural``. Pure; reads only structural fields.
    """
    moments = collapse_episodes_by_moment(episodes)
    agreeing_episodes = 0
    violations = 0
    for episode in moments:
        moment = episode.moment
        moment_violations = 0
        if moment.schema.capacity_used != len(moment.items):
            moment_violations += 1
        if moment.schema.omitted_count < len(moment.excluded):
            moment_violations += 1
        if moment.schema.capacity_used > moment.max_items:
            moment_violations += 1
        violations += moment_violations
        if moment_violations == 0:
            agreeing_episodes += 1

    verdict = _score(
        episodes=len(moments),
        violations=violations,
        fired=len(moments) > 0,
    )
    return SchemaAgreementReading(
        episodes=len(moments),
        agreeing_episodes=agreeing_episodes,
        violations=violations,
        verdict=verdict,
    )


def evaluate_temporal_continuity(
    episodes: Sequence[ConsciousEpisode],
) -> TemporalContinuityReading:
    """Score temporal continuity from each moment's continuity marker and link.

    Collapses rows to moments (:func:`collapse_episodes_by_moment`) first, then
    folds each moment: buckets it by continuity kind and flags a violation when a
    ``genesis`` moment carries a ``predecessor_id`` or a non-genesis moment has
    none — a non-genesis moment must either name a predecessor or carry a typed
    gap reason. Verdict rules — no moments → ``not_observed``; any violation →
    ``refuted``; a lone genesis moment with no successor → ``architectural`` (the
    chain never advanced); otherwise ``behaviorally_supported``. Pure; reads only
    the continuity markers.
    """
    moments = collapse_episodes_by_moment(episodes)
    linked_episodes = 0
    gap_episodes = 0
    genesis_episodes = 0
    violations = 0
    for episode in moments:
        moment = episode.moment
        if moment.continuity == "genesis":
            genesis_episodes += 1
            if moment.predecessor_id is not None:
                violations += 1
        else:
            if moment.continuity == "linked":
                linked_episodes += 1
            else:
                gap_episodes += 1
            if moment.predecessor_id is None:
                violations += 1

    verdict = _score(
        episodes=len(moments),
        violations=violations,
        fired=(linked_episodes + gap_episodes) > 0,
    )
    return TemporalContinuityReading(
        episodes=len(moments),
        linked_episodes=linked_episodes,
        gap_episodes=gap_episodes,
        genesis_episodes=genesis_episodes,
        violations=violations,
        verdict=verdict,
    )


def evaluate_causal_reach(
    episodes: Sequence[ConsciousEpisode],
) -> CausalReachReading:
    """Score causal reach from each moment's validated decision citations.

    Collapses rows to moments (:func:`collapse_episodes_by_moment`) first, then
    folds each moment: counts the task and plan ids the downstream decisions
    cited, and flags a provenance violation when a citation is not backed by the
    citing consumer's delivery receipt — cited plans without an ``evolution``
    receipt, or cited tasks without a ``task_generation`` receipt. A citation no
    broadcast backs is a provenance breach, not evidence, so it is excluded from
    the ``reaching_episodes`` numerator as well as scored as a violation; only a
    receipt-backed citation earns a moment reach credit.

    The reading measures **declared decision uptake**, not counterfactual
    causality: a citation proves a decision *named* an item it was shown, never
    that the decision would have differed without it. So the verdict is derived
    through the shared :func:`_score` ladder with ``fired=False``, deliberately
    capping this indicator at ``architectural`` — no moments → ``not_observed``;
    any unbacked citation → ``refuted``; an otherwise healthy trace →
    ``architectural``. The indicator matrix reserves ``behaviorally_supported``
    for a controlled ablation showing decisions differ when the admitted set
    differs; until that harness exists the counts are reported while the verdict
    stays honest. Pure; reads only the citation and receipt fields.
    """
    moments = collapse_episodes_by_moment(episodes)
    reaching_episodes = 0
    cited_tasks = 0
    cited_plans = 0
    violations = 0
    for episode in moments:
        consumers = {receipt.consumer for receipt in episode.receipts}
        tasks_backed = bool(episode.cited_task_ids) and "task_generation" in consumers
        plans_backed = bool(episode.cited_plan_ids) and "evolution" in consumers
        if tasks_backed or plans_backed:
            reaching_episodes += 1
        cited_tasks += len(episode.cited_task_ids)
        cited_plans += len(episode.cited_plan_ids)
        if episode.cited_plan_ids and not plans_backed:
            violations += 1
        if episode.cited_task_ids and not tasks_backed:
            violations += 1

    verdict = _score(
        episodes=len(moments), violations=violations, fired=False,
    )
    return CausalReachReading(
        episodes=len(moments),
        reaching_episodes=reaching_episodes,
        cited_tasks=cited_tasks,
        cited_plans=cited_plans,
        violations=violations,
        verdict=verdict,
    )


def _score(*, episodes: int, violations: int, fired: bool) -> WorkspaceVerdict:
    """The shared verdict ladder every reading scores against.

    Empty ledger reads ``not_observed``; any violation ``refuted`` (so corrupt
    provenance can never read as support); an unexercised-but-present behaviour
    ``architectural``; otherwise ``behaviorally_supported``. Sole owner of the
    ordering so all six readings stay consistent.
    """
    if episodes == 0:
        return "not_observed"
    if violations > 0:
        return "refuted"
    if not fired:
        return "architectural"
    return "behaviorally_supported"
