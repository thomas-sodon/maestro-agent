"""Tests for the eager whole-fleet legacy→``state.db`` migration (proposal 106).

Mirrors ``tests/test_state_store.py``: every SQLite instance is
``StateStore(":memory:")`` (a real in-memory DB, never a ``mem_fs`` path) while
the legacy sidecars are populated under one fake ``memory_dir`` through the
``mem_fs`` fixture using the real record/append APIs. The converter
(:meth:`StateStore.migrate_from_legacy`) is exercised end-to-end: parity per
store, idempotency, skip-already-seeded, empty-legacy, the cost-sample
exclusion, summary fidelity, and a registry-coverage guard.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from maestro.audit_checks import (
    GateActivation,
    JsonImpactReviewSurfacedStore,
    JsonlGateActivationLedger,
    load_all_gate_activations,
    load_all_impact_review_surfaced,
)
from maestro.audit_checks_calibration import (
    JsonlPredictionLedger,
    JsonlPremiseLedger,
    PremiseObservation,
    load_all_predictions,
    load_all_premise_observations,
)
from maestro.audit_checks_phantom import (
    JsonOrphanedCureStore,
    JsonUnconfirmedOwnLandingStore,
    load_all_orphaned_cure_seen,
    load_all_unconfirmed_landing_seen,
)
from maestro.audit_checks_settlement import (
    JsonlSettledVerdictsLedger,
    SettledVerdictRecord,
    load_all_settled_verdicts,
)
from maestro.conscious_workspace import (
    MAX_MOMENT_CHARS,
    MAX_MOMENT_ITEMS,
    AttentionSchema,
    ConsciousEpisode,
    ConsciousMoment,
    JsonlConsciousEpisodeLedger,
    build_workspace_item,
    load_all_conscious_episodes,
)
from maestro.deed_ledger import DeedType, load_all_deeds, record_deed
from maestro.directive_ledger import (
    DroppedDirective,
    JsonlDroppedDirectiveLedger,
    load_all_dropped_directives,
)
from maestro.escalation_bridge import (
    JsonEscalationLedger,
    load_all_escalation_standdowns,
)
from maestro.lever_capture import (
    JsonlLeverAttemptsLedger,
    build_lever_attempt,
    load_all_lever_attempts,
)
from maestro.memory import (
    AuditResult,
    JsonlAuditHistoryLedger,
    JsonlQuestionResolutionLedger,
    JsonlRejectionHistoryLedger,
    JsonlVersionArchiveLedger,
    QuestionResolution,
    RejectionRecord,
    VersionRecord,
    load_all_audit_history,
    load_all_question_resolutions,
    load_all_rejection_history,
    load_all_versions,
)
from maestro.outcome_grounding import (
    JsonMergeRequestServicedStore,
    load_all_serviced_merge_requests,
)
from maestro.pr_ledger import (
    JsonPRLedger,
    PRLedgerEntry,
    load_all_pr_ledger_entries,
)
from maestro.remediation_tracker import (
    JsonlStandDownVerdictLedger,
    JsonRemediationTrackerStore,
    TrackerState,
    load_all_fetch_truth_markers,
    load_all_obligation_forks,
    load_all_preflight_passes,
    load_all_provenance_records,
    load_all_stand_down_records,
    load_tracker_state,
    log_pending_pr_provenance,
    record_fetch_truth,
    record_obligation_fork_truth,
    record_preflight_pass,
    record_stand_down,
)
from maestro.settlement_record_reconciler import (
    JsonlSettlementReconciliationLedger,
    SettlementReconciliationRecord,
    load_all_settlement_reconciliations,
)
from maestro.state_store import (
    StateMigrationSummary,
    StateStore,
    _remediation_tracker_rows,
)
from maestro.stats import load_all_backlog_snapshots, record_owned_backlog_snapshot
from maestro.task_queue import QueuedTask, TaskQueue

#: The single fake ``memory_dir`` every populate call writes into (mem_fs).
MEM = Path("/fake/mem")


def _make_episode(moment_id: str, item_id: str) -> ConsciousEpisode:
    """Build a minimal but valid conscious episode for the migration fixtures."""
    item = build_workspace_item(
        item_id=item_id, source_kind="audit_finding", source_ref="ref",
        observed_at="2026-04-26T00:00:00+00:00", trust="mechanical_observation",
        confidence=0.9, expires_at=None, summary="a bounded summary",
    )
    schema = AttentionSchema(
        focus="focus", uncertainty=0.1, capacity_used=1, omitted_count=0,
        permission_summary="all", blind_spots=(),
    )
    moment = ConsciousMoment(
        moment_id=moment_id, predecessor_id=None, continuity="genesis",
        assembled_at="2026-04-26T00:00:00+00:00", policy_version="v1",
        items=(item,), excluded=(), schema=schema,
        candidate_hash="ch", selection_hash="sh",
        max_items=MAX_MOMENT_ITEMS, max_chars=MAX_MOMENT_CHARS,
    )
    return ConsciousEpisode(
        moment=moment, receipts=(), cited_task_ids=(), cited_plan_ids=(),
        recorded_at="2026-04-26T00:00:00+00:00",
    )


def _populate_all(d: Path) -> None:
    """Write two legacy records into every seedable sidecar under *d* (mem_fs)."""
    # 1 task_queue
    TaskQueue(d / "task_queue.json").save([
        QueuedTask(task_id="t1", goal="g1",
                   created_at="2026-01-01T00:00:00+00:00",
                   last_touched_at="2026-01-01T00:00:00+00:00"),
        QueuedTask(task_id="t2", goal="g2",
                   created_at="2026-01-01T00:00:00+00:00",
                   last_touched_at="2026-01-01T00:00:00+00:00"),
    ])
    # 2 deed_ledger
    record_deed(d, DeedType.PR_CLOSE, 1, "first", now="2026-01-01T00:00:01+00:00")
    record_deed(d, DeedType.MERGE, 2, "second", now="2026-01-01T00:00:02+00:00")
    # 3 lever_attempts
    lev = JsonlLeverAttemptsLedger(d)
    lev.record(build_lever_attempt("mark_plan_obsolete", ["a", "r"], True, True))
    lev.record(build_lever_attempt("mark_plan_obsolete", ["b", "r"], False, False))
    # 4 audit_history
    aud = JsonlAuditHistoryLedger(d)
    aud.record(AuditResult(iteration=1, timestamp="2026-08-15T00:00:00Z",
                           findings=[], rules_fired=[]))
    aud.record(AuditResult(iteration=2, timestamp="2026-08-16T00:00:00Z",
                           findings=[], rules_fired=[]))
    # 5 rejection_history
    rej = JsonlRejectionHistoryLedger(d)
    for pid in ("ep_1", "ep_2"):
        rej.record(RejectionRecord(plan_id=pid, title="Example plan",
                                   category="capability", priority="capability",
                                   rejection_reason="Too aggressive",
                                   timestamp="2026-04-23T12:00:00+00:00",
                                   rejection_category="scope_too_large"))
    # 6 version_archive
    ver = JsonlVersionArchiveLedger(d)
    for vid, pid in (("v_1", "ep_1"), ("v_2", "ep_2")):
        ver.record(VersionRecord(
            version_id=vid, plan_id=pid, plan_title="Example plan",
            commit_sha="abc123", merged_at="2026-04-23T12:00:00+00:00",
            window_tasks_total=5, window_tasks_succeeded=4, window_tasks_failed=1,
            window_sdk_cost_usd=1.25, window_sdk_calls=10, window_cache_hit_rate=0.5,
            window_rate_limit_hits=0, window_error_count=0,
            quality_gate_pass=True, notes=None))
    # 7 pr_ledger
    JsonPRLedger(d).mutate(lambda m: m.update({
        1: PRLedgerEntry(pr_number=1, branch="b1", observed_head="h1"),
        2: PRLedgerEntry(pr_number=2, branch="b2", observed_head="h2"),
    }))
    # 8 remediation_tracker (2 PR keys -> summary count == 2)
    JsonRemediationTrackerStore(d).persist(
        TrackerState({"1": [True], "2": [False]}, {}, {}, {}, 2))
    # 9 escalation_standdowns
    JsonEscalationLedger(d).mutate(lambda m: m.update({
        1: {"head_sha": "h1", "delivered_at": "t", "serviced_comment_ids": []},
        2: {"head_sha": "h2", "delivered_at": "t", "serviced_comment_ids": [7]},
    }))
    # 10 question_resolutions
    qr = JsonlQuestionResolutionLedger(d)
    for rid in ("r1", "r2"):
        qr.record(QuestionResolution(resolution_id=rid, iteration=1,
                                     timestamp="2026-04-26T00:00:00+00:00",
                                     question="What am I capable of?", task_id="t1",
                                     outcome="success", disposition="removed",
                                     finding="I can refactor.", refined_to=None))
    # 11 gate_activations
    ga = JsonlGateActivationLedger(d)
    ga.append(GateActivation(gate="g1", action="blocked",
                             timestamp="2026-04-26T00:00:00+00:00", detail="d1"))
    ga.append(GateActivation(gate="g2", action="directive_set",
                             timestamp="2026-04-26T00:00:01+00:00", detail="d2"))
    # 12 predictions
    pre = JsonlPredictionLedger(d)
    pre.append({"task_id": "t1", "predicted_outcome": "success",
                "timestamp": "2026-04-26T00:00:00+00:00"})
    pre.append({"task_id": "t2", "predicted_outcome": "partial",
                "timestamp": "2026-04-26T00:00:01+00:00"})
    # 13 owned_backlog
    record_owned_backlog_snapshot(d, 1, 40, now="2026-01-01T00:00:01+00:00")
    record_owned_backlog_snapshot(d, 2, 37, now="2026-01-01T00:00:02+00:00")
    # 14 pr_provenance
    log_pending_pr_provenance(d, 100, "tend_route")
    log_pending_pr_provenance(d, 200, "tend_route")
    # 15 settlement_reconciliations
    srec = JsonlSettlementReconciliationLedger(d)
    srec.append(SettlementReconciliationRecord(pr_number=1, reconciler="self_model",
                                               cleared=True, detail="note",
                                               at_iteration=1))
    srec.append(SettlementReconciliationRecord(pr_number=2, reconciler="pr_ledger",
                                               cleared=False, detail=None,
                                               at_iteration=2))
    # 16 obligation_fork (2 distinct task_ids)
    record_obligation_fork_truth("t1", 1, "h1", "r1", "b1", d,
                                 remote_is_ancestor=False)
    record_obligation_fork_truth("t2", 2, "h2", "r2", "b2", d,
                                 remote_is_ancestor=True)
    # 17 fetch_truth (2 distinct task_ids)
    record_fetch_truth("t1", 1, False, d, head_sha="h1")
    record_fetch_truth("t2", 2, True, d, head_sha="h2")
    # 18 preflight_passes (2 distinct PRs)
    record_preflight_pass(1, "h1", d)
    record_preflight_pass(2, "h2", d)
    # 19 dropped_directives
    ddl = JsonlDroppedDirectiveLedger(d)
    ddl.offer(DroppedDirective(text="a", source="s", pr_numbers=(1,), shas=(),
                               recorded_at="2026-07-09T00:00:00+00:00"))
    ddl.offer(DroppedDirective(text="b", source="s", pr_numbers=(2,), shas=(),
                               recorded_at="2026-07-09T00:00:00+00:00"))
    # 20 settled_verdicts (2 distinct PRs)
    JsonlSettledVerdictsLedger(d).append([
        SettledVerdictRecord(pr_number=100, settled=True, kind="merged",
                             iteration=1, verdict_task_id=None),
        SettledVerdictRecord(pr_number=200, settled=True, kind="closed",
                             iteration=1, verdict_task_id=None),
    ])
    # 21 stand_down_verdicts (2 verdicts, NO releases)
    sdv = JsonlStandDownVerdictLedger(d)
    record_stand_down(1, "a", sdv, now="t1", branch_ahead_of_origin=True)
    record_stand_down(2, "x", sdv, now="t2")
    # 22 unconfirmed_landing (2 keys)
    JsonUnconfirmedOwnLandingStore(d).prune_and_touch(["aaa", "bbb"], 5)
    # 23 orphaned_cure (2 keys)
    JsonOrphanedCureStore(d).prune_and_touch(["ep1", "ep2"], 5)
    # 24 impact_review_surfaced (2 members)
    JsonImpactReviewSurfacedStore(d).record(["v1", "v2"])
    # 25 mergeable_serviced (2 distinct PR keys)
    ms = JsonMergeRequestServicedStore(d)
    ms.record_serviced(2647, "abc")
    ms.record_serviced(9001, "abc")
    # 26 premise_observations (registry position: right after gate_activations)
    pmo = JsonlPremiseLedger(d)
    pmo.append(PremiseObservation(task_id="t1", referenced=2, missing=1,
                                  timestamp="2026-04-26T00:00:00+00:00"))
    pmo.append(PremiseObservation(task_id="t2", referenced=1, missing=0,
                                  timestamp="2026-04-26T00:00:01+00:00"))
    # 27 conscious_episodes (2 episodes)
    cel = JsonlConsciousEpisodeLedger(d)
    cel.append(_make_episode("m1", "i1"))
    cel.append(_make_episode("m2", "i2"))


#: Per-store legacy row count. Every populated store carries exactly two
#: top-level entries, so each maps to ``2`` — the value the converter reports
#: (``len(loaded)`` for collection stores, ``len(_remediation_tracker_rows())``
#: for the tracker, ``len(TaskQueue.load())`` for the queue).
COUNT_LOADERS: dict[str, Callable[[Path], int]] = {
    "task_queue": lambda d: len(TaskQueue(d / "task_queue.json").load()),
    "deed_ledger": lambda d: len(load_all_deeds(d)),
    "lever_attempts": lambda d: len(load_all_lever_attempts(d)),
    "audit_history": lambda d: len(load_all_audit_history(d)),
    "rejection_history": lambda d: len(load_all_rejection_history(d)),
    "version_archive": lambda d: len(load_all_versions(d)),
    "pr_ledger": lambda d: len(load_all_pr_ledger_entries(d)),
    "remediation_tracker": lambda d: len(
        _remediation_tracker_rows(load_tracker_state(d)),
    ),
    "escalation_standdowns": lambda d: len(load_all_escalation_standdowns(d)),
    "question_resolutions": lambda d: len(load_all_question_resolutions(d)),
    "gate_activations": lambda d: len(load_all_gate_activations(d)),
    "premise_observations": lambda d: len(load_all_premise_observations(d)),
    "predictions": lambda d: len(load_all_predictions(d)),
    "owned_backlog": lambda d: len(load_all_backlog_snapshots(d)),
    "pr_provenance": lambda d: len(load_all_provenance_records(d)),
    "settlement_reconciliations": lambda d: len(
        load_all_settlement_reconciliations(d),
    ),
    "obligation_fork": lambda d: len(load_all_obligation_forks(d)),
    "fetch_truth": lambda d: len(load_all_fetch_truth_markers(d)),
    "preflight_passes": lambda d: len(load_all_preflight_passes(d)),
    "dropped_directives": lambda d: len(load_all_dropped_directives(d)),
    "settled_verdicts": lambda d: len(load_all_settled_verdicts(d)),
    "stand_down_verdicts": lambda d: len(load_all_stand_down_records(d)),
    "unconfirmed_landing": lambda d: len(load_all_unconfirmed_landing_seen(d)),
    "orphaned_cure": lambda d: len(load_all_orphaned_cure_seen(d)),
    "impact_review_surfaced": lambda d: len(load_all_impact_review_surfaced(d)),
    "mergeable_serviced": lambda d: len(load_all_serviced_merge_requests(d)),
    "conscious_episodes": lambda d: len(load_all_conscious_episodes(d)),
}

#: The full seedable-store set, in registry order.
ALL_STORES: list[str] = list(COUNT_LOADERS)

#: Stores whose SQLite read returns the loader's exact shape, so a post-migrate
#: deep-equality parity assertion is valid.
VALUE_PARITY: list[
    tuple[str, Callable[[StateStore], object], Callable[[Path], object]]
] = [
    ("task_queue",
     lambda s: s.load_task_queue(),
     lambda d: TaskQueue(d / "task_queue.json").load()),
    ("lever_attempts",
     lambda s: s.load_lever_attempts(),
     lambda d: load_all_lever_attempts(d)),
    ("audit_history",
     lambda s: s.load_audit_results(),
     lambda d: load_all_audit_history(d)),
    ("rejection_history",
     lambda s: s.load_rejections(),
     lambda d: load_all_rejection_history(d)),
    ("version_archive",
     lambda s: s.load_versions(),
     lambda d: load_all_versions(d)),
    ("pr_ledger",
     lambda s: s.load_pr_ledger(),
     lambda d: {e.pr_number: e for e in load_all_pr_ledger_entries(d)}),
    ("remediation_tracker",
     lambda s: s.load_remediation_tracker(),
     lambda d: load_tracker_state(d)),
    ("escalation_standdowns",
     lambda s: s.load_escalation_standdowns(),
     lambda d: load_all_escalation_standdowns(d)),
    ("question_resolutions",
     lambda s: s.load_question_resolutions(),
     lambda d: load_all_question_resolutions(d)),
    ("gate_activations",
     lambda s: s.load_gate_activations(),
     lambda d: load_all_gate_activations(d)),
    ("premise_observations",
     lambda s: s.load_premise_observations(),
     lambda d: load_all_premise_observations(d)),
    ("predictions",
     lambda s: s.load_predictions(),
     lambda d: load_all_predictions(d)),
    ("settlement_reconciliations",
     lambda s: s.load_settlement_reconciliations(),
     lambda d: load_all_settlement_reconciliations(d)),
    ("stand_down_verdicts",
     lambda s: s.stand_down_verdict_rows(),
     lambda d: load_all_stand_down_records(d)),
    ("unconfirmed_landing",
     lambda s: s.load_unconfirmed_landing_seen(),
     lambda d: load_all_unconfirmed_landing_seen(d)),
    ("orphaned_cure",
     lambda s: s.load_orphaned_cure_seen(),
     lambda d: load_all_orphaned_cure_seen(d)),
    ("impact_review_surfaced",
     lambda s: s.load_impact_review_surfaced(),
     lambda d: load_all_impact_review_surfaced(d)),
    ("conscious_episodes",
     lambda s: s.load_conscious_episodes(),
     lambda d: load_all_conscious_episodes(d)),
]

#: Stores with no shape-matching SQLite list read — verified by count parity
#: (above) plus a predicate spot-check that the migrated data is queryable.
COUNT_CHECKS: list[tuple[str, Callable[[StateStore], bool]]] = [
    ("deed_ledger", lambda s: s.count_deeds_since() == 2),
    ("owned_backlog", lambda s: len(s.recent_backlog_snapshots(10)) == 2),
    ("pr_provenance", lambda s: len(s.recent_provenance(10)) == 2),
    ("obligation_fork",
     lambda s: s.get_obligation_fork_for_task("t1") is not None),
    ("fetch_truth", lambda s: s.has_fetch_truth_for_task("t1") is True),
    ("preflight_passes", lambda s: s.should_skip_preflight(1, "h1") is True),
    ("settled_verdicts", lambda s: 100 in s.effective_settled_prs()),
    ("dropped_directives",
     lambda s: s._conn.execute(
         "SELECT COUNT(*) AS n FROM dropped_directives",
     ).fetchone()["n"] == 2),
    ("mergeable_serviced",
     lambda s: s.is_merge_request_serviced(2647, "abc") is True),
]


# ---------------------------------------------------------------------------
# Whole-fleet seed: counts, markers, summary
# ---------------------------------------------------------------------------


def test_migrate_seeds_every_store_with_reported_counts(
    mem_fs: dict[str, str],
) -> None:
    """One pass seeds every store, reports its row count, and stamps its marker."""
    _populate_all(MEM)
    store = StateStore(":memory:")
    summary = store.migrate_from_legacy(MEM)
    # Summary fidelity: each store's reported count equals its legacy row count.
    assert summary.seeded == {name: COUNT_LOADERS[name](MEM) for name in ALL_STORES}
    assert summary.already_seeded == []
    # Every registry marker is now durably stamped.
    for entry in store._migration_entries(MEM):
        assert entry.is_seeded() is True


@pytest.mark.parametrize(
    ("name", "sqlite_read", "legacy_read"),
    VALUE_PARITY,
    ids=[spec[0] for spec in VALUE_PARITY],
)
def test_migrate_round_trip_parity(
    name: str,
    sqlite_read: Callable[[StateStore], object],
    legacy_read: Callable[[Path], object],
    mem_fs: dict[str, str],
) -> None:
    """Each shape-matching store round-trips equal to its legacy loader."""
    _populate_all(MEM)
    store = StateStore(":memory:")
    store.migrate_from_legacy(MEM)
    assert sqlite_read(store) == legacy_read(MEM)


@pytest.mark.parametrize(
    ("name", "check"),
    COUNT_CHECKS,
    ids=[spec[0] for spec in COUNT_CHECKS],
)
def test_migrate_lands_queryable_data_for_count_stores(
    name: str,
    check: Callable[[StateStore], bool],
    mem_fs: dict[str, str],
) -> None:
    """Each count-only store's migrated data is queryable after the pass."""
    _populate_all(MEM)
    store = StateStore(":memory:")
    store.migrate_from_legacy(MEM)
    assert check(store) is True


# ---------------------------------------------------------------------------
# Idempotency and skip-already-seeded
# ---------------------------------------------------------------------------


def test_migrate_is_idempotent_second_run_seeds_nothing(
    mem_fs: dict[str, str],
) -> None:
    """A second pass reports every store already-seeded and seeds zero."""
    _populate_all(MEM)
    store = StateStore(":memory:")
    store.migrate_from_legacy(MEM)
    second = store.migrate_from_legacy(MEM)
    assert second.seeded == {}
    assert set(second.already_seeded) == set(ALL_STORES)


def test_migrate_skips_a_store_seeded_lazily_first(
    mem_fs: dict[str, str],
) -> None:
    """A store an adapter already seeded is reported already-seeded, not doubled."""
    _populate_all(MEM)
    store = StateStore(":memory:")
    # Simulate a lazy first-access seed of one store before the eager pass.
    store.seed_deeds_once(load_all_deeds(MEM))
    summary = store.migrate_from_legacy(MEM)
    assert "deed_ledger" in summary.already_seeded
    assert "deed_ledger" not in summary.seeded
    assert set(summary.seeded) == set(ALL_STORES) - {"deed_ledger"}
    # The pre-seeded data is present exactly once (never re-counted / doubled).
    assert store.count_deeds_since() == 2


# ---------------------------------------------------------------------------
# Empty legacy and the cost-sample exclusion
# ---------------------------------------------------------------------------


def test_migrate_empty_legacy_seeds_zero_but_stamps_markers(
    mem_fs: dict[str, str],
) -> None:
    """An absent-sidecar memory_dir seeds zero rows yet stamps every marker."""
    empty = Path("/fake/empty")
    store = StateStore(":memory:")
    summary = store.migrate_from_legacy(empty)
    assert summary.seeded == {name: 0 for name in ALL_STORES}
    assert summary.already_seeded == []
    for entry in store._migration_entries(empty):
        assert entry.is_seeded() is True


def test_migrate_leaves_cost_samples_untouched(mem_fs: dict[str, str]) -> None:
    """Cost samples are never in the registry and the cost table stays empty."""
    _populate_all(MEM)
    store = StateStore(":memory:")
    summary = store.migrate_from_legacy(MEM)
    assert "cost_samples" not in summary.seeded
    assert "cost_samples" not in summary.already_seeded
    assert store.recent_cost_samples(100) == []


# ---------------------------------------------------------------------------
# Registry-coverage guard
# ---------------------------------------------------------------------------


def test_registry_covers_every_is_seeded_predicate() -> None:
    """The registry's predicate set equals every ``is_*_seeded`` on StateStore."""
    store = StateStore(":memory:")
    covered = {
        entry.is_seeded.__name__ for entry in store._migration_entries(MEM)
    }
    introspected = {
        name
        for name in dir(StateStore)
        if name.startswith("is_") and name.endswith("_seeded")
    }
    assert covered == introspected


def test_registry_store_names_are_unique_and_complete() -> None:
    """Every store name is distinct and the fleet has exactly 27 stores."""
    store = StateStore(":memory:")
    names = [entry.store_name for entry in store._migration_entries(MEM)]
    assert len(names) == len(set(names))
    assert len(names) == 27


# ---------------------------------------------------------------------------
# Summary rendering
# ---------------------------------------------------------------------------


def test_summary_render_lists_seeded_and_already_seeded() -> None:
    """render() folds sorted seeded counts and already-seeded names into one line."""
    summary = StateMigrationSummary(
        seeded={"beta": 2, "alpha": 1}, already_seeded=["zeta", "gamma"],
    )
    rendered = summary.render()
    assert "seeded 2 store(s), 3 row(s) [alpha=1, beta=2]" in rendered
    assert "already-seeded 2 store(s) [gamma, zeta]" in rendered


def test_summary_render_handles_all_empty() -> None:
    """render() reports ``none`` for both halves when nothing moved."""
    summary = StateMigrationSummary(seeded={}, already_seeded=[])
    rendered = summary.render()
    assert "seeded 0 store(s), 0 row(s) [none]" in rendered
    assert "already-seeded 0 store(s) [none]" in rendered
