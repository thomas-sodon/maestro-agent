"""Tests for the offline conscious-episode evidence reader (proposal 130).

Every fixture is built in memory — no ``mem_fs``, no I/O — because the six
evaluators are pure folds over :class:`~maestro.conscious_workspace.ConsciousEpisode`
sequences. Each verdict path carries a negative control (the healthy case sits
next to its refuted/architectural sibling), and the ``refuted`` cases exercise
every distinct cap/integrity violation so no branch passes vacuously. Fixture
builders are reused from ``tests.test_conscious_workspace``.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path
from typing import Any, get_args

import pytest

from maestro import consciousness_eval
from maestro.conscious_workspace import (
    MAX_ITEM_SUMMARY_CHARS,
    MAX_MOMENT_ITEMS,
    AttentionSchema,
    ConsciousEpisode,
    ConsciousMoment,
    ExcludedCandidate,
    WorkspaceItem,
    compute_candidate_hash,
    compute_selection_hash,
)
from maestro.consciousness_eval import (
    CausalReachReading,
    LimitedAccessReading,
    ReceiptedBroadcastReading,
    RecurrenceReading,
    SchemaAgreementReading,
    TemporalContinuityReading,
    WorkspaceVerdict,
    collapse_episodes_by_moment,
    evaluate_causal_reach,
    evaluate_limited_access,
    evaluate_receipted_broadcast,
    evaluate_recurrence,
    evaluate_schema_agreement,
    evaluate_temporal_continuity,
)
from tests.test_conscious_workspace import (
    _episode,
    _excluded,
    _item,
    _moment,
    _receipt,
    _schema,
)

_AUTHORIZED = ("contemplation", "evolution", "task_generation", "task_execution")


def _valid_moment(
    items: tuple[WorkspaceItem, ...] | None = None,
    excluded: tuple[ExcludedCandidate, ...] | None = None,
    schema: AttentionSchema | None = None,
    **overrides: Any,
) -> ConsciousMoment:
    """A moment whose stored hashes match a recomputation (healthy provenance).

    Any explicit *overrides* (e.g. a tampered ``selection_hash``) win over the
    computed defaults, so a single call can seed either a healthy or a corrupt
    moment.
    """
    items = (_item(),) if items is None else items
    excluded = (_excluded(),) if excluded is None else excluded
    schema = _schema() if schema is None else schema
    base: dict[str, Any] = dict(
        items=items,
        excluded=excluded,
        schema=schema,
        selection_hash=compute_selection_hash(items, schema),
        candidate_hash=compute_candidate_hash(items, excluded),
    )
    base.update(overrides)
    return _moment(**base)


def _valid_episode(
    moment: ConsciousMoment | None = None,
    consumers: tuple[str, ...] = ("contemplation",),
    **overrides: Any,
) -> ConsciousEpisode:
    """An episode carrying valid receipts (authorized consumers, matching hash)."""
    moment = _valid_moment() if moment is None else moment
    receipts = tuple(
        _receipt(
            consumer=c, moment_id=moment.moment_id, moment_hash=moment.selection_hash,
        )
        for c in consumers
    )
    return _episode(moment=moment, receipts=receipts, **overrides)


# ---------------------------------------------------------------------------
# Closed vocabulary
# ---------------------------------------------------------------------------


def test_workspace_verdict_is_closed() -> None:
    assert set(get_args(WorkspaceVerdict)) == {
        "not_observed",
        "architectural",
        "behaviorally_supported",
        "refuted",
    }


# ---------------------------------------------------------------------------
# evaluate_limited_access
# ---------------------------------------------------------------------------


def test_limited_access_empty_is_not_observed() -> None:
    reading = evaluate_limited_access([])
    assert isinstance(reading, LimitedAccessReading)
    assert reading.verdict == "not_observed"
    assert reading.episodes == 0
    assert reading.violations == 0


def test_limited_access_competition_within_caps_is_supported() -> None:
    reading = evaluate_limited_access([_episode(moment=_valid_moment())])
    assert reading.verdict == "behaviorally_supported"
    assert reading.episodes == 1
    assert reading.competition_episodes == 1
    assert reading.admitted == 1
    assert reading.excluded == 1
    assert reading.violations == 0


def test_limited_access_no_competition_is_architectural() -> None:
    moment = _valid_moment(excluded=())
    reading = evaluate_limited_access([_episode(moment=moment, receipts=())])
    assert reading.verdict == "architectural"
    assert reading.competition_episodes == 0
    assert reading.excluded == 0


def test_limited_access_capacity_used_over_cap_is_refuted() -> None:
    moment = _valid_moment(schema=_schema(capacity_used=MAX_MOMENT_ITEMS + 1))
    reading = evaluate_limited_access([_episode(moment=moment)])
    assert reading.verdict == "refuted"
    assert reading.violations == 1


def test_limited_access_admitted_count_over_cap_is_refuted() -> None:
    items = tuple(
        _item(item_id=f"i{n}", summary="x") for n in range(MAX_MOMENT_ITEMS + 1)
    )
    moment = _valid_moment(
        items=items, excluded=(), schema=_schema(capacity_used=1),
    )
    reading = evaluate_limited_access([_episode(moment=moment, receipts=())])
    assert reading.verdict == "refuted"
    assert reading.violations == 1
    assert reading.admitted == MAX_MOMENT_ITEMS + 1


def test_limited_access_summary_chars_over_cap_is_refuted() -> None:
    # Eight max-length summaries (8 * 320 = 2560) exceed MAX_MOMENT_CHARS (2400)
    # while the item count (8) stays under MAX_MOMENT_ITEMS (12).
    items = tuple(
        _item(item_id=f"i{n}", summary="a" * MAX_ITEM_SUMMARY_CHARS)
        for n in range(8)
    )
    moment = _valid_moment(
        items=items, excluded=(), schema=_schema(capacity_used=8),
    )
    reading = evaluate_limited_access([_episode(moment=moment, receipts=())])
    assert reading.verdict == "refuted"
    assert reading.violations == 1


def test_limited_access_tampered_selection_hash_is_refuted() -> None:
    moment = _valid_moment(selection_hash="tampered")
    reading = evaluate_limited_access([_episode(moment=moment)])
    assert reading.verdict == "refuted"
    assert reading.violations == 1


def test_limited_access_tampered_candidate_hash_is_refuted() -> None:
    moment = _valid_moment(candidate_hash="tampered")
    reading = evaluate_limited_access([_episode(moment=moment)])
    assert reading.verdict == "refuted"
    assert reading.violations == 1


# ---------------------------------------------------------------------------
# evaluate_receipted_broadcast
# ---------------------------------------------------------------------------


def test_receipted_broadcast_empty_is_not_observed() -> None:
    reading = evaluate_receipted_broadcast([])
    assert isinstance(reading, ReceiptedBroadcastReading)
    assert reading.verdict == "not_observed"
    assert reading.episodes == 0
    assert reading.receipts == 0


def test_receipted_broadcast_full_coverage_is_supported() -> None:
    reading = evaluate_receipted_broadcast(
        [_valid_episode(consumers=_AUTHORIZED)],
    )
    assert reading.verdict == "behaviorally_supported"
    assert reading.episodes == 1
    assert reading.receipted_episodes == 1
    assert reading.receipts == len(_AUTHORIZED)
    assert reading.consumers_covered == len(_AUTHORIZED)
    assert reading.violations == 0


def test_receipted_broadcast_no_receipts_is_architectural() -> None:
    reading = evaluate_receipted_broadcast(
        [_episode(moment=_valid_moment(), receipts=())],
    )
    assert reading.verdict == "architectural"
    assert reading.receipts == 0
    assert reading.receipted_episodes == 0
    assert reading.consumers_covered == 0


def test_receipted_broadcast_unknown_consumer_is_refuted() -> None:
    moment = _valid_moment()
    episode = _episode(
        moment=moment,
        receipts=(_receipt(consumer="planner", moment_hash=moment.selection_hash),),
    )
    reading = evaluate_receipted_broadcast([episode])
    assert reading.verdict == "refuted"
    assert reading.violations == 1
    # An unauthorized consumer is never counted as coverage.
    assert reading.consumers_covered == 0


def test_receipted_broadcast_hash_mismatch_is_refuted() -> None:
    moment = _valid_moment()
    episode = _episode(
        moment=moment,
        receipts=(_receipt(consumer="contemplation", moment_hash="tampered"),),
    )
    reading = evaluate_receipted_broadcast([episode])
    assert reading.verdict == "refuted"
    assert reading.violations == 1
    # The consumer is authorized even though the moment hash was tampered.
    assert reading.consumers_covered == 1


def test_receipted_broadcast_moment_id_mismatch_is_refuted() -> None:
    """A receipt whose moment_id names a different moment than the one it rides is
    an integrity violation — the defense-in-depth check beyond the hash."""
    moment = _valid_moment()
    episode = _episode(
        moment=moment,
        receipts=(
            _receipt(
                consumer="contemplation",
                moment_id="other-moment",
                moment_hash=moment.selection_hash,
            ),
        ),
    )
    reading = evaluate_receipted_broadcast([episode])
    assert reading.verdict == "refuted"
    assert reading.violations == 1
    # Hash and consumer are both valid; only the moment_id disagrees.
    assert reading.consumers_covered == 1


# ---------------------------------------------------------------------------
# evaluate_recurrence
# ---------------------------------------------------------------------------


def test_recurrence_empty_is_not_observed() -> None:
    reading = evaluate_recurrence([])
    assert isinstance(reading, RecurrenceReading)
    assert reading.verdict == "not_observed"
    assert reading.episodes == 0
    assert reading.lineage_items == 0
    assert reading.violations == 0


def test_recurrence_task_outcome_lineage_is_supported() -> None:
    """A lineage-bearing item sourced from a mechanical task outcome is the
    recurrence loop firing legitimately — a verified outcome fed back."""
    item = _item(
        source_kind="task_outcome", trust="mechanical_observation",
        lineage=("prior-i1",),
    )
    reading = evaluate_recurrence([_episode(moment=_valid_moment(items=(item,)))])
    assert reading.verdict == "behaviorally_supported"
    assert reading.episodes == 1
    assert reading.recurrence_episodes == 1
    assert reading.lineage_items == 1
    assert reading.violations == 0


def test_recurrence_no_lineage_is_architectural() -> None:
    """Moments exist but no item carries lineage yet — the machinery is present
    but the recurrence loop has never fired."""
    reading = evaluate_recurrence([_episode(moment=_valid_moment())])
    assert reading.verdict == "architectural"
    assert reading.episodes == 1
    assert reading.recurrence_episodes == 0
    assert reading.lineage_items == 0
    assert reading.violations == 0


def test_recurrence_self_authored_lineage_is_refuted() -> None:
    """A lineage-bearing item that is *not* a mechanical task outcome is a
    provenance breach: self-authored narration must never perpetuate itself."""
    item = _item(
        source_kind="self_model", trust="agent_derived", lineage=("prior-i1",),
    )
    reading = evaluate_recurrence([_episode(moment=_valid_moment(items=(item,)))])
    assert reading.verdict == "refuted"
    assert reading.lineage_items == 1
    assert reading.violations == 1


# ---------------------------------------------------------------------------
# evaluate_schema_agreement
# ---------------------------------------------------------------------------


def test_schema_agreement_empty_is_not_observed() -> None:
    reading = evaluate_schema_agreement([])
    assert isinstance(reading, SchemaAgreementReading)
    assert reading.verdict == "not_observed"
    assert reading.episodes == 0
    assert reading.violations == 0


def test_schema_agreement_consistent_schema_is_supported() -> None:
    """The schema's load and omission agree with the selection trace:
    capacity_used == admitted, omitted_count >= excluded, within max_items."""
    moment = _valid_moment(schema=_schema(capacity_used=1, omitted_count=1))
    reading = evaluate_schema_agreement([_episode(moment=moment)])
    assert reading.verdict == "behaviorally_supported"
    assert reading.episodes == 1
    assert reading.agreeing_episodes == 1
    assert reading.violations == 0


def test_schema_agreement_capacity_mismatch_is_refuted() -> None:
    """capacity_used disagrees with the admitted count — the schema misreports its
    own load."""
    moment = _valid_moment(schema=_schema(capacity_used=5, omitted_count=1))
    reading = evaluate_schema_agreement([_episode(moment=moment)])
    assert reading.verdict == "refuted"
    assert reading.agreeing_episodes == 0
    assert reading.violations == 1


def test_schema_agreement_omitted_below_excluded_is_refuted() -> None:
    """omitted_count under the recorded-exclusion count — the schema under-reports
    what it left out (the exclusion trace is a capped subset, so >= is required)."""
    excluded = (_excluded(item_id="x1"), _excluded(item_id="x2"))
    moment = _valid_moment(
        excluded=excluded, schema=_schema(capacity_used=1, omitted_count=1),
    )
    reading = evaluate_schema_agreement([_episode(moment=moment)])
    assert reading.verdict == "refuted"
    assert reading.violations == 1


def test_schema_agreement_capacity_over_max_items_is_refuted() -> None:
    """capacity_used over the moment's max_items — a schema claiming more load than
    the moment's own cap allows. Isolated (admitted count matches capacity_used) so
    only the over-cap check fires."""
    items = tuple(
        _item(item_id=f"i{n}", summary="x") for n in range(MAX_MOMENT_ITEMS + 1)
    )
    moment = _valid_moment(
        items=items, excluded=(),
        schema=_schema(capacity_used=MAX_MOMENT_ITEMS + 1, omitted_count=0),
    )
    reading = evaluate_schema_agreement([_episode(moment=moment)])
    assert reading.verdict == "refuted"
    assert reading.violations == 1


# ---------------------------------------------------------------------------
# evaluate_temporal_continuity
# ---------------------------------------------------------------------------


def test_temporal_continuity_empty_is_not_observed() -> None:
    reading = evaluate_temporal_continuity([])
    assert isinstance(reading, TemporalContinuityReading)
    assert reading.verdict == "not_observed"
    assert reading.episodes == 0
    assert reading.violations == 0


def test_temporal_continuity_lone_genesis_is_architectural() -> None:
    """A single genesis moment with no predecessor and no successor — the chain
    has a valid root but never advanced."""
    moment = _valid_moment(continuity="genesis", predecessor_id=None)
    reading = evaluate_temporal_continuity([_episode(moment=moment)])
    assert reading.verdict == "architectural"
    assert reading.genesis_episodes == 1
    assert reading.linked_episodes == 0
    assert reading.gap_episodes == 0
    assert reading.violations == 0


def test_temporal_continuity_genesis_then_linked_is_supported() -> None:
    """A genesis root followed by a linked successor naming its predecessor — an
    honest, advancing chain."""
    genesis = _valid_moment(moment_id="m1", continuity="genesis", predecessor_id=None)
    linked = _valid_moment(moment_id="m2", continuity="linked", predecessor_id="m1")
    reading = evaluate_temporal_continuity(
        [_episode(moment=genesis), _episode(moment=linked)],
    )
    assert reading.verdict == "behaviorally_supported"
    assert reading.episodes == 2
    assert reading.genesis_episodes == 1
    assert reading.linked_episodes == 1
    assert reading.violations == 0


def test_temporal_continuity_typed_gap_is_supported() -> None:
    """A crash_gap successor naming its predecessor is an honest, typed gap — the
    loop crashed but the chain still links back (covers the gap bucket)."""
    moment = _valid_moment(moment_id="m2", continuity="crash_gap", predecessor_id="m1")
    reading = evaluate_temporal_continuity([_episode(moment=moment)])
    assert reading.verdict == "behaviorally_supported"
    assert reading.gap_episodes == 1
    assert reading.linked_episodes == 0
    assert reading.violations == 0


def test_temporal_continuity_genesis_with_predecessor_is_refuted() -> None:
    """A genesis moment must not name a predecessor — a genesis carrying one is a
    dishonest continuity marker."""
    moment = _valid_moment(continuity="genesis", predecessor_id="m0")
    reading = evaluate_temporal_continuity([_episode(moment=moment)])
    assert reading.verdict == "refuted"
    assert reading.genesis_episodes == 1
    assert reading.violations == 1


def test_temporal_continuity_nongenesis_without_predecessor_is_refuted() -> None:
    """A non-genesis moment must either name a predecessor or be a typed gap — a
    linked moment with no predecessor is an unlinked chain break."""
    moment = _valid_moment(moment_id="m2", continuity="linked", predecessor_id=None)
    reading = evaluate_temporal_continuity([_episode(moment=moment)])
    assert reading.verdict == "refuted"
    assert reading.linked_episodes == 1
    assert reading.violations == 1


# ---------------------------------------------------------------------------
# collapse_episodes_by_moment — the two-rows-per-moment guard
# ---------------------------------------------------------------------------


def test_collapse_keeps_last_row_per_moment() -> None:
    """A bootstrap row (empty receipts) plus its superseding row sharing a
    moment_id collapse to one moment carrying the *later* row's receipts.

    This pins the read-time guard against double-counting a moment that left two
    ledger rows: without the collapse the two evaluators would fold the bootstrap
    and superseding rows separately, reporting two moments and reading the
    receipt-less bootstrap as "never broadcast".
    """
    moment = _valid_moment()
    bootstrap = _episode(
        moment=moment, receipts=(), cited_task_ids=(), cited_plan_ids=(),
        recorded_at="2026-01-01T00:00:00+00:00",
    )
    superseding = _valid_episode(
        moment=moment, recorded_at="2026-01-01T00:05:00+00:00",
    )

    collapsed = collapse_episodes_by_moment([bootstrap, superseding])
    assert len(collapsed) == 1
    assert collapsed[0].receipts == superseding.receipts
    assert collapsed[0].receipts  # the later row's receipts, not the empty tuple

    # Both evaluators fold moments, not rows: one moment, broadcast once.
    broadcast = evaluate_receipted_broadcast([bootstrap, superseding])
    assert broadcast.episodes == 1
    assert broadcast.receipted_episodes == 1
    assert broadcast.receipts == len(superseding.receipts)
    assert broadcast.verdict == "behaviorally_supported"

    limited = evaluate_limited_access([bootstrap, superseding])
    assert limited.episodes == 1
    assert limited.competition_episodes == 1


def test_collapse_preserves_first_seen_moment_order() -> None:
    """Distinct moments survive in first-seen order — the collapse is a fold, not
    a filter, so two different moments are never merged."""
    first = _valid_moment(moment_id="m1")
    second = _valid_moment(moment_id="m2")
    collapsed = collapse_episodes_by_moment(
        [_episode(moment=first), _episode(moment=second)],
    )
    assert len(collapsed) == 2
    assert [e.moment.moment_id for e in collapsed] == ["m1", "m2"]


# ---------------------------------------------------------------------------
# evaluate_causal_reach
# ---------------------------------------------------------------------------


def test_causal_reach_empty_is_not_observed() -> None:
    reading = evaluate_causal_reach([])
    assert isinstance(reading, CausalReachReading)
    assert reading.verdict == "not_observed"
    assert reading.episodes == 0
    assert reading.reaching_episodes == 0
    assert reading.cited_tasks == 0
    assert reading.cited_plans == 0
    assert reading.violations == 0


def test_causal_reach_backed_citations_cap_at_architectural() -> None:
    """A task citation backed by a ``task_generation`` receipt counts, and the
    verdict stops at ``architectural``: declared uptake is not the matrix's
    behavioural-support bar (that needs a counterfactual ablation)."""
    reading = evaluate_causal_reach([
        _valid_episode(
            consumers=("task_generation", "evolution"),
            cited_task_ids=("t1", "t2"),
            cited_plan_ids=("p1",),
        ),
    ])
    assert reading.verdict == "architectural"
    assert reading.episodes == 1
    assert reading.reaching_episodes == 1
    assert reading.cited_tasks == 2
    assert reading.cited_plans == 1
    assert reading.violations == 0


def test_causal_reach_no_citations_is_architectural_with_zero_reach() -> None:
    """The negative control for the row above: a broadcast moment no decision
    cited is still ``architectural`` — unexercised, not refuted."""
    reading = evaluate_causal_reach([
        _valid_episode(
            consumers=("task_generation",), cited_task_ids=(), cited_plan_ids=(),
        ),
    ])
    assert reading.verdict == "architectural"
    assert reading.episodes == 1
    assert reading.reaching_episodes == 0
    assert reading.cited_tasks == 0
    assert reading.violations == 0


def test_causal_reach_plan_citation_without_evolution_receipt_is_refuted() -> None:
    """A cited plan whose ``evolution`` consumer never received the moment is a
    provenance breach — a citation no delivery backs earns no credit."""
    reading = evaluate_causal_reach([
        _valid_episode(
            consumers=("task_generation",),
            cited_task_ids=(),
            cited_plan_ids=("p1",),
        ),
    ])
    assert reading.verdict == "refuted"
    assert reading.violations == 1
    assert reading.cited_plans == 1
    # The unbacked citation is indicted, never credited: it stays out of the
    # reach numerator so one citation cannot both count and be a violation.
    assert reading.reaching_episodes == 0


def test_causal_reach_task_citation_without_task_receipt_is_refuted() -> None:
    """The sibling breach: a cited task with no ``task_generation`` receipt."""
    reading = evaluate_causal_reach([
        _valid_episode(
            consumers=("evolution",),
            cited_task_ids=("t1",),
            cited_plan_ids=(),
        ),
    ])
    assert reading.verdict == "refuted"
    assert reading.violations == 1
    assert reading.cited_tasks == 1


def test_causal_reach_mixed_backing_credits_only_the_backed_citation() -> None:
    """A moment citing both kinds but receipted by only one consumer earns reach
    credit from the backed half while the unbacked half still scores a
    violation — the numerator and the violation count stay disjoint."""
    reading = evaluate_causal_reach([
        _valid_episode(
            consumers=("task_generation",),
            cited_task_ids=("t1",),
            cited_plan_ids=("p1",),
        ),
    ])
    assert reading.reaching_episodes == 1
    assert reading.cited_tasks == 1
    assert reading.cited_plans == 1
    assert reading.violations == 1
    assert reading.verdict == "refuted"


def test_causal_reach_folds_bootstrap_and_superseding_to_one_moment() -> None:
    """The bootstrap row (no receipts, no citations) and its superseding row
    sharing a moment_id fold to one moment carrying the later row's citations —
    without the collapse the bootstrap would read as an uncited moment."""
    moment = _valid_moment()
    bootstrap = _episode(
        moment=moment, receipts=(), cited_task_ids=(), cited_plan_ids=(),
        recorded_at="2026-01-01T00:00:00+00:00",
    )
    superseding = _valid_episode(
        moment=moment,
        consumers=("task_generation",),
        cited_task_ids=("t1",),
        cited_plan_ids=(),
        recorded_at="2026-01-01T00:05:00+00:00",
    )

    reading = evaluate_causal_reach([bootstrap, superseding])
    assert reading.episodes == 1
    assert reading.reaching_episodes == 1
    assert reading.cited_tasks == 1
    assert reading.violations == 0
    assert reading.verdict == "architectural"


# ---------------------------------------------------------------------------
# Module invariants
# ---------------------------------------------------------------------------


def test_readings_are_frozen() -> None:
    """The readings are frozen dataclasses — a folded count cannot be mutated in
    place after the evaluator returns it."""
    limited = evaluate_limited_access([_valid_episode()])
    broadcast = evaluate_receipted_broadcast([_valid_episode()])
    reach = evaluate_causal_reach([_valid_episode()])
    with pytest.raises(dataclasses.FrozenInstanceError):
        limited.episodes = 99  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        broadcast.episodes = 99  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        reach.episodes = 99  # type: ignore[misc]


def test_module_imports_only_conscious_workspace_from_maestro() -> None:
    """AST cycle-free guard: the reader imports nothing from ``maestro`` other than
    ``conscious_workspace``, so it stays a leaf the CLI imports beside the ledger
    backends (mirrors ``test_self_calibration_module_stays_cycle_free``). An AST
    walk over the source is authoritative even against an import buried in a
    function body.
    """
    source = Path(consciousness_eval.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    maestro_imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("maestro"):
                    maestro_imports.add(alias.name)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.module.startswith("maestro")
        ):
            maestro_imports.add(node.module)
    assert maestro_imports == {"maestro.conscious_workspace"}
