"""Plan restatement, re-cited source guard and the mechanical pending-plan sweeps;
split out of ``tests/test_memory.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from maestro import memory as memory_module
from maestro.lever_capture import JsonlLeverAttemptsLedger
from maestro.memory import (
    EvolutionPlan,
    Memory,
    find_pending_plan_cluster,
    is_plan_restatement,
    is_source_task_recited,
)
from tests.test_memory import (
    _LEVER_LEDGER,
    FAKE_DIR,
    _make_memory,
    _plan,
)


def test_get_live_plans_surfaces_approved_and_implemented(
    mem_fs: dict[str, str],
) -> None:
    """get_live_plans returns the approved/implemented rows get_pending_plans omits."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_plan("p-pending", "t", "r", status="pending_review"))
    mem.save_evolution_plan(_plan("p-approved", "t", "r", status="approved"))
    mem.save_evolution_plan(_plan("p-implemented", "t", "r", status="implemented"))
    mem.save_evolution_plan(_plan("p-merged", "t", "r", status="merged"))
    mem.save_evolution_plan(_plan("p-rejected", "t", "r", status="rejected"))

    pending_ids = {p.plan_id for p in mem.get_pending_plans()}
    live = mem.get_live_plans()
    live_by_id = {lp.plan_id: lp for lp in live}

    assert pending_ids == {"p-pending"}
    assert set(live_by_id) == {"p-pending", "p-approved", "p-implemented"}
    assert live_by_id["p-approved"].status == "approved"
    assert live_by_id["p-implemented"].status == "implemented"


def test_get_live_plan_records_surfaces_approved_and_implemented(
    mem_fs: dict[str, str],
) -> None:
    """get_live_plan_records returns the live EvolutionPlan rows, excluding terminal."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_plan("p-pending", "t", "r", status="pending_review"))
    mem.save_evolution_plan(_plan("p-approved", "t", "r", status="approved"))
    mem.save_evolution_plan(_plan("p-implemented", "t", "r", status="implemented"))
    mem.save_evolution_plan(_plan("p-merged", "t", "r", status="merged"))
    mem.save_evolution_plan(_plan("p-rejected", "t", "r", status="rejected"))
    mem.save_evolution_plan(_plan("p-failed", "t", "r", status="failed"))
    mem.save_evolution_plan(_plan("p-obsolete", "t", "r", status="obsolete"))

    records = mem.get_live_plan_records()

    # Full EvolutionPlan rows (not the frozen LivePlan projection).
    assert all(isinstance(p, EvolutionPlan) for p in records)
    assert {p.plan_id for p in records} == {
        "p-pending", "p-approved", "p-implemented",
    }
    # The approved/implemented rows are exactly the ones get_pending_plans omits.
    pending_ids = {p.plan_id for p in mem.get_pending_plans()}
    assert {p.plan_id for p in records} - pending_ids == {
        "p-approved", "p-implemented",
    }


def test_is_plan_restatement_blocks_near_verbatim() -> None:
    """A near-verbatim title+rationale matches the pending plan it restates."""
    pending = [_plan(
        "p1",
        "Escalate unlanded remediation to Thomas",
        "Deliver one merge-or-close message when a lineage stalls past threshold",
    )]
    match = is_plan_restatement(
        "Escalate unlanded remediation to Thomas once",
        "Deliver a single merge-or-close message when the lineage stalls past threshold",
        pending,
    )
    assert match is not None
    assert match.plan_id == "p1"


def test_is_plan_restatement_allows_distinct() -> None:
    """A genuinely distinct plan does not match any pending plan."""
    pending = [_plan(
        "p1",
        "Escalate unlanded remediation to Thomas",
        "Deliver one merge-or-close message when a lineage stalls past threshold",
    )]
    match = is_plan_restatement(
        "Add prometheus latency histogram to dashboard router",
        "Instrument request duration buckets so operators can chart tail latency",
        pending,
    )
    assert match is None


def test_is_plan_restatement_honours_caller_corpus_regardless_of_status() -> None:
    """The predicate compares whatever corpus it is handed, whatever the status.

    Inverts the pre-proposal-100 contract: a non-``pending_review`` plan in the
    corpus is no longer skipped internally, so an identical candidate now
    matches it. Which statuses are eligible to be restated is the caller's
    choice, not this function's.
    """
    title = "Escalate unlanded remediation to Thomas"
    rationale = "Deliver one merge-or-close message when a lineage stalls"
    for status in ("merged", "rejected", "obsolete", "implemented", "approved"):
        corpus = [_plan("p1", title, rationale, status=status)]
        match = is_plan_restatement(title, rationale, corpus)
        assert match is not None
        assert match.plan_id == "p1"


def test_is_plan_restatement_threshold_boundary() -> None:
    """Just-below 0.5 Jaccard passes; at/above 0.5 blocks.

    Candidate content tokens: {alpha, bravo, charlie, delta}. The pending
    plan shares exactly two of them and adds two of its own, giving a
    Jaccard of 2/6 = 0.33 < 0.5 — below the threshold, so it passes.
    Adding a third shared token raises the overlap to 3/5 = 0.6 ≥ 0.5.
    """
    candidate_title = "alpha bravo"
    candidate_rationale = "charlie delta"
    below = [_plan("p1", "alpha bravo", "echo foxtrot")]
    assert is_plan_restatement(candidate_title, candidate_rationale, below) is None
    at_or_above = [_plan("p2", "alpha bravo", "charlie foxtrot")]
    match = is_plan_restatement(candidate_title, candidate_rationale, at_or_above)
    assert match is not None
    assert match.plan_id == "p2"


def test_is_plan_restatement_empty_candidate_returns_none() -> None:
    """A candidate with no content tokens never blocks (empty token set)."""
    pending = [_plan("p1", "alpha bravo", "charlie delta")]
    assert is_plan_restatement("", "", pending) is None


def test_is_plan_restatement_custom_threshold() -> None:
    """A higher *threshold* lets a borderline overlap through; a lower one blocks.

    Candidate tokens {alpha, bravo, charlie, delta} vs pending
    {alpha, bravo, charlie, foxtrot}: Jaccard 3/5 = 0.6. Above-0.6 thresholds
    pass; at-or-below thresholds block.
    """
    pending = [_plan("p1", "alpha bravo", "charlie foxtrot")]
    assert is_plan_restatement(
        "alpha bravo", "charlie delta", pending, threshold=0.7,
    ) is None
    match = is_plan_restatement(
        "alpha bravo", "charlie delta", pending, threshold=0.6,
    )
    assert match is not None
    assert match.plan_id == "p1"


# ---------------------------------------------------------------------------
# Re-cited-source-branch guard — live_cited_source_task_ids +
# is_source_task_recited
# ---------------------------------------------------------------------------


def _plan_with_source(
    plan_id: str,
    source_task_id: str | None,
    status: str = "pending_review",
) -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id,
        title="t",
        category="capability",
        rationale="r",
        proposed_changes=[],
        files_affected=[],
        expected_impact="",
        status=status,  # type: ignore[arg-type]
        source_task_id=source_task_id,
        created_at="2025-01-01T00:00:00Z",
    )


def test_live_cited_source_task_ids_includes_pending_approved_and_implemented(
    mem_fs: dict[str, str],
) -> None:
    """pending_review/approved/implemented citations count; nulls are excluded."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_plan_with_source("p-pend", "task-A", "pending_review"))
    mem.save_evolution_plan(_plan_with_source("p-appr", "task-C", "approved"))
    mem.save_evolution_plan(_plan_with_source("p-impl", "task-B", "implemented"))
    # A live plan citing no branch contributes nothing.
    mem.save_evolution_plan(_plan_with_source("p-null", None, "pending_review"))

    assert mem.live_cited_source_task_ids() == {"task-A", "task-B", "task-C"}


def test_live_cited_source_task_ids_excludes_terminal(
    mem_fs: dict[str, str],
) -> None:
    """rejected/failed/obsolete/merged citations are terminal, not 'live'.

    ``merged`` is landed-and-done — excluded like the other terminal statuses
    and unlike ``approved`` (still-in-flight, asserted live above).
    """
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_plan_with_source("p-rej", "task-R", "rejected"))
    mem.save_evolution_plan(_plan_with_source("p-fail", "task-F", "failed"))
    mem.save_evolution_plan(_plan_with_source("p-obs", "task-O", "obsolete"))
    mem.save_evolution_plan(_plan_with_source("p-merged", "task-M", "merged"))

    assert mem.live_cited_source_task_ids() == set()


def test_live_cited_source_task_ids_empty_when_no_citations(
    mem_fs: dict[str, str],
) -> None:
    """A backlog whose live plans cite no branch yields an empty set."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_plan_with_source("p-pend", None, "pending_review"))
    mem.save_evolution_plan(_plan_with_source("p-impl", None, "implemented"))

    assert mem.live_cited_source_task_ids() == set()


def test_is_source_task_recited_true_for_live_citation() -> None:
    """The predicate is True when a live plan already cites the branch.

    ``approved`` is a still-open claim (queued for implementation), so it is
    live alongside ``pending_review``/``implemented``.
    """
    plans = [
        _plan_with_source("p-pend", "task-A", "pending_review"),
        _plan_with_source("p-appr", "task-C", "approved"),
        _plan_with_source("p-impl", "task-B", "implemented"),
    ]
    assert is_source_task_recited("task-A", plans) is True
    assert is_source_task_recited("task-C", plans) is True
    assert is_source_task_recited("task-B", plans) is True


def test_is_source_task_recited_false_for_terminal_only_citation() -> None:
    """A branch cited only by rejected/failed/obsolete/merged plans is re-citable."""
    plans = [
        _plan_with_source("p-rej", "task-R", "rejected"),
        _plan_with_source("p-fail", "task-F", "failed"),
        _plan_with_source("p-obs", "task-O", "obsolete"),
        _plan_with_source("p-merged", "task-M", "merged"),
    ]
    assert is_source_task_recited("task-R", plans) is False
    assert is_source_task_recited("task-F", plans) is False
    assert is_source_task_recited("task-O", plans) is False
    assert is_source_task_recited("task-M", plans) is False


def test_is_source_task_recited_false_for_null_or_uncited() -> None:
    """A null source_task_id, and an id no live plan cites, are not recited."""
    plans = [_plan_with_source("p-pend", "task-A", "pending_review")]
    assert is_source_task_recited(None, plans) is False
    assert is_source_task_recited("", plans) is False
    assert is_source_task_recited("task-Z", plans) is False


def test_find_duplicate_pending_plan_returns_id(mem_fs: dict[str, str]) -> None:
    """A >=threshold overlap with a pending plan returns that plan's id."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_plan(
        "p1",
        "Escalate unlanded remediation to Thomas",
        "Deliver one merge-or-close message when a lineage stalls past threshold",
    ))
    dup_id = mem.find_duplicate_pending_plan(
        "Escalate unlanded remediation to Thomas once",
        "Deliver a single merge-or-close message when the lineage stalls past threshold",
    )
    assert dup_id == "p1"


def test_find_duplicate_pending_plan_distinct_returns_none(
    mem_fs: dict[str, str],
) -> None:
    """A genuinely distinct candidate matches no pending plan."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_plan(
        "p1",
        "Escalate unlanded remediation to Thomas",
        "Deliver one merge-or-close message when a lineage stalls past threshold",
    ))
    dup_id = mem.find_duplicate_pending_plan(
        "Add prometheus latency histogram to dashboard router",
        "Instrument request duration buckets so operators can chart tail latency",
    )
    assert dup_id is None


def test_find_duplicate_pending_plan_empty_set_returns_none(
    mem_fs: dict[str, str],
) -> None:
    """With no pending plans there is nothing to duplicate."""
    mem = _make_memory(mem_fs)
    assert mem.find_duplicate_pending_plan("alpha bravo", "charlie delta") is None


def test_find_duplicate_pending_plan_ignores_terminal_status(
    mem_fs: dict[str, str],
) -> None:
    """An obsolete twin is terminal, not a live plan — no duplicate (proposal 100)."""
    mem = _make_memory(mem_fs)
    title = "Escalate unlanded remediation to Thomas"
    rationale = "Deliver one merge-or-close message when a lineage stalls past threshold"
    mem.save_evolution_plan(_plan("p1", title, rationale, status="obsolete"))
    assert mem.find_duplicate_pending_plan(title, rationale) is None


def test_find_duplicate_pending_plan_respects_threshold(
    mem_fs: dict[str, str],
) -> None:
    """A high *threshold* lets a borderline candidate through the guard."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_plan("p1", "alpha bravo", "charlie foxtrot"))
    # Jaccard 0.6 — below a 0.7 cutoff, so no duplicate is reported.
    assert mem.find_duplicate_pending_plan(
        "alpha bravo", "charlie delta", threshold=0.7,
    ) is None
    assert mem.find_duplicate_pending_plan(
        "alpha bravo", "charlie delta", threshold=0.6,
    ) == "p1"


def test_find_duplicate_pending_plan_catches_approved_live(
    mem_fs: dict[str, str],
) -> None:
    """An approved near-restatement is caught: the corpus spans live plans (proposal 100)."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_plan(
        "p1",
        "Escalate unlanded remediation to Thomas",
        "Deliver one merge-or-close message when a lineage stalls past threshold",
        status="approved",
    ))
    candidate_title = "Escalate unlanded remediation to Thomas once"
    candidate_body = (
        "Deliver a single merge-or-close message when the lineage stalls past threshold"
    )
    # The live corpus spans approved plans, so the approved twin is refused.
    assert mem.find_duplicate_pending_plan(candidate_title, candidate_body) == "p1"


def test_find_duplicate_pending_plan_exclude_ids_surfaces_next_match(
    mem_fs: dict[str, str],
) -> None:
    """``exclude_ids`` drops the named plans from the corpus, so a co-colliding
    match that is *not* excluded still surfaces (proposal 118 sibling exemption).

    The candidate near-restates both ``p1`` (first in file order) and ``p2``.
    Excluding ``p1`` must still return ``p2`` — the first-match-only accessor
    cannot hide a non-excluded duplicate behind an excluded one — while
    excluding both yields ``None``.
    """
    mem = _make_memory(mem_fs)
    title = "Escalate unlanded remediation to Thomas"
    rationale = "Deliver one merge-or-close message when a lineage stalls past threshold"
    mem.save_evolution_plan(_plan("p1", title, rationale))
    mem.save_evolution_plan(_plan("p2", title, rationale))
    candidate_title = "Escalate unlanded remediation to Thomas once"
    candidate_body = (
        "Deliver a single merge-or-close message when the lineage stalls past threshold"
    )

    # No exclusion → the first match (file order) is p1.
    assert mem.find_duplicate_pending_plan(candidate_title, candidate_body) == "p1"
    # Excluding p1 → the co-colliding p2 still surfaces.
    assert mem.find_duplicate_pending_plan(
        candidate_title, candidate_body, exclude_ids={"p1"},
    ) == "p2"
    # Excluding both → nothing left to duplicate.
    assert mem.find_duplicate_pending_plan(
        candidate_title, candidate_body, exclude_ids={"p1", "p2"},
    ) is None


# Five themed near-duplicates: each shares the candidate's dominant theme
# tokens (stand/awaiting/approval/remediation) but carries enough unique
# vocabulary to stay below the pairwise Jaccard cutoff — the exact backlog
# shape the cluster guard exists to catch.
_THEME_PLANS = [
    (
        "c1",
        "Stand on awaiting approval remediation lineages quietly",
        "Stop the merge-or-close churn and accept stillness this iteration",
    ),
    (
        "c2",
        "Stand on awaiting approval remediation without reworking again",
        "Halt repeated escalation drafting; the lineage already carries a verdict",
    ),
    (
        "c3",
        "Stand on awaiting approval remediation pending Thomas ruling",
        "Cease minting fresh sensors that only describe the same standoff",
    ),
    (
        "c4",
        "Stand on awaiting approval remediation backlog deliberately",
        "Decline another costume signature; choose empty hands over motion",
    ),
    (
        "c5",
        "Stand on awaiting approval remediation lineages once more",
        "Refuse the treadmill of rephrasing one unlanded standoff differently",
    ),
]


def _save_theme_plans(mem: Memory) -> None:
    for plan_id, title, rationale in _THEME_PLANS:
        mem.save_evolution_plan(_plan(plan_id, title, rationale))


def test_find_theme_cluster_pending_plans_positive_cluster(
    mem_fs: dict[str, str],
) -> None:
    """Five themed near-duplicates surface as a cluster of size 5."""
    mem = _make_memory(mem_fs)
    _save_theme_plans(mem)
    cluster = mem.find_theme_cluster_pending_plans(
        "Stand awaiting approval remediation",
        "Stand awaiting approval remediation once again",
    )
    assert len(cluster) == 5
    assert sorted(p.plan_id for p in cluster) == ["c1", "c2", "c3", "c4", "c5"]


def test_find_theme_cluster_pending_plans_spans_live_statuses(
    mem_fs: dict[str, str],
) -> None:
    """The themed cluster spans approved/implemented plans — the live corpus (100)."""
    mem = _make_memory(mem_fs)
    # The same five-plan theme, but approved/implemented rather than pending.
    live_statuses = ["approved", "implemented", "approved", "implemented", "approved"]
    for (plan_id, title, rationale), status in zip(
        _THEME_PLANS, live_statuses, strict=True,
    ):
        mem.save_evolution_plan(_plan(plan_id, title, rationale, status=status))
    args = (
        "Stand awaiting approval remediation",
        "Stand awaiting approval remediation once again",
    )
    # The live corpus surfaces the full themed cluster of approved/implemented plans.
    cluster = mem.find_theme_cluster_pending_plans(*args)
    assert sorted(p.plan_id for p in cluster) == ["c1", "c2", "c3", "c4", "c5"]


def test_find_theme_cluster_pending_plans_distinct_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """A genuinely distinct candidate joins no themed cluster."""
    mem = _make_memory(mem_fs)
    _save_theme_plans(mem)
    cluster = mem.find_theme_cluster_pending_plans(
        "Add prometheus latency histogram to dashboard router",
        "Instrument request duration buckets so operators chart tail latency",
    )
    assert cluster == []


def test_find_theme_cluster_pending_plans_empty_content_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """Empty or stop-word-only candidate text yields no cluster."""
    mem = _make_memory(mem_fs)
    _save_theme_plans(mem)
    assert mem.find_theme_cluster_pending_plans("", "") == []
    # Stop-words and <=2-char tokens are filtered out, leaving no signature.
    assert mem.find_theme_cluster_pending_plans("the a an", "to of by") == []


def test_find_theme_cluster_pending_plans_short_candidate_not_overmatched(
    mem_fs: dict[str, str],
) -> None:
    """A terse, genuinely-novel candidate is not clustered on incidental words.

    Regression for the directional-containment over-match: with only three
    dominant tokens, two *unrelated* large pending plans that incidentally
    contain those words would each clear the 0.6 containment cutoff
    (3-of-3 = 1.0, 2-of-3 = 0.67) and land the novel plan ``obsolete`` without
    review. The ``THEME_CLUSTER_MIN_CANDIDATE_TOKENS`` floor suppresses that:
    a candidate below the floor carries no trustworthy theme signature.
    """
    mem = _make_memory(mem_fs)
    # Two substantial, mutually-unrelated pending plans that merely *mention*
    # the candidate's few words in passing.
    mem.save_evolution_plan(_plan(
        "u1",
        "Harden dashboard CSRF middleware ordering",
        "The dashboard auth stack should cache bearer tokens and refactor email "
        "notification templates so security headers wrap every 401 from the "
        "public API surface during reconnect storms.",
    ))
    mem.save_evolution_plan(_plan(
        "u2",
        "Speed up planner replan execution path",
        "The planner rebuilds execution groups each step; cache the topo order "
        "and add an email digest template for common step shapes to avoid "
        "redundant replanning work across the loop.",
    ))
    # Terse novel candidate -> tokens {cache, email, templates}, below the floor.
    cluster = mem.find_theme_cluster_pending_plans(
        "Cache email templates", "",
    )
    assert cluster == []


def test_find_theme_cluster_pending_plans_floor_boundary(
    mem_fs: dict[str, str],
) -> None:
    """Below-floor candidates never cluster; at-floor candidates still can.

    A three-token candidate fully contained by every themed plan is suppressed
    by the floor, while a four-token candidate (at the floor) clusters as
    before — proving the floor disables only the untrustworthy small-set case.
    """
    mem = _make_memory(mem_fs)
    for i in range(2):
        mem.save_evolution_plan(_plan(
            f"f{i}",
            "Stand awaiting approval remediation lineage now",
            "Stand awaiting approval remediation lineage now and again",
        ))
    # Three tokens {stand, awaiting, approval}: fully contained, but below floor.
    assert mem.find_theme_cluster_pending_plans(
        "Stand awaiting approval", "",
    ) == []
    # Four tokens {stand, awaiting, approval, remediation}: at the floor,
    # fully contained -> clusters (size 2 meets the default threshold).
    assert len(mem.find_theme_cluster_pending_plans(
        "Stand awaiting approval remediation", "",
    )) == 2


def test_find_theme_cluster_pending_plans_respects_threshold(
    mem_fs: dict[str, str],
) -> None:
    """A *threshold* above the cluster size suppresses the match."""
    mem = _make_memory(mem_fs)
    _save_theme_plans(mem)
    title = "Stand awaiting approval remediation"
    description = "Stand awaiting approval remediation once again"
    # Exactly five themed plans share the theme: threshold 5 returns them,
    # threshold 6 leaves the candidate free to mint.
    assert len(
        mem.find_theme_cluster_pending_plans(title, description, threshold=5)
    ) == 5
    assert mem.find_theme_cluster_pending_plans(
        title, description, threshold=6,
    ) == []


def test_find_theme_cluster_pending_plans_fraction_boundary(
    mem_fs: dict[str, str],
) -> None:
    """Containment must strictly exceed *cluster_fraction* to count.

    Candidate tokens are {stand, awaiting, approval, remediation, alpha}
    (5 tokens); each themed plan contains the first four, giving exactly
    0.8 containment. A 0.8 fraction (``>`` is strict) excludes them; a
    0.6 fraction includes them.
    """
    mem = _make_memory(mem_fs)
    _save_theme_plans(mem)
    title = "Stand awaiting approval remediation alpha"
    assert mem.find_theme_cluster_pending_plans(
        title, "", cluster_fraction=0.8,
    ) == []
    assert len(
        mem.find_theme_cluster_pending_plans(title, "", cluster_fraction=0.6)
    ) == 5


def test_find_theme_cluster_pending_plans_skips_empty_token_plan(
    mem_fs: dict[str, str],
) -> None:
    """A pending plan with no content tokens is skipped, not counted."""
    mem = _make_memory(mem_fs)
    _save_theme_plans(mem)
    # A blank-bodied pending plan contributes no tokens and must not join.
    mem.save_evolution_plan(_plan("blank", "", ""))
    cluster = mem.find_theme_cluster_pending_plans(
        "Stand awaiting approval remediation",
        "Stand awaiting approval remediation once again",
    )
    assert "blank" not in {p.plan_id for p in cluster}
    assert len(cluster) == 5


def test_find_theme_cluster_pending_plans_no_pending_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """With nothing pending there is no cluster to join."""
    mem = _make_memory(mem_fs)
    assert mem.find_theme_cluster_pending_plans(
        "Stand awaiting approval remediation", "",
    ) == []


def test_find_pending_plan_cluster_returns_above_threshold(
    mem_fs: dict[str, str],
) -> None:
    """Returns exactly the pending plans whose Jaccard meets the threshold.

    Candidate tokens {alpha, bravo, charlie, delta}. ``p_above1`` shares
    three (Jaccard 3/5 = 0.6) and ``p_above2`` shares three as well
    (3/5 = 0.6) — both ≥ 0.5. ``p_below`` shares one (1/7 ≈ 0.14 < 0.5) and
    is excluded.
    """
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_plan("p_above1", "alpha bravo", "charlie echo"))
    mem.save_evolution_plan(_plan("p_above2", "alpha bravo", "charlie foxtrot"))
    mem.save_evolution_plan(_plan("p_below", "alpha golf", "hotel india"))
    cluster = find_pending_plan_cluster(
        mem, "alpha bravo\ncharlie delta", threshold=0.5,
    )
    assert sorted(p.plan_id for p in cluster) == ["p_above1", "p_above2"]


def test_find_pending_plan_cluster_spans_live_excludes_terminal(
    mem_fs: dict[str, str],
) -> None:
    """Live plans (pending/approved/implemented) join; terminal ones never do (100).

    The module-level cluster reads the live-work corpus, so an ``approved`` or
    ``implemented`` twin clusters while an ``obsolete``/``merged``/``rejected``
    twin — terminal, not live — is excluded even when byte-identical.
    """
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_plan("p_pending", "alpha bravo", "charlie delta"))
    mem.save_evolution_plan(
        _plan("p_approved", "alpha bravo", "charlie delta", status="approved"),
    )
    mem.save_evolution_plan(
        _plan("p_implemented", "alpha bravo", "charlie delta", status="implemented"),
    )
    for status in ("obsolete", "merged", "rejected"):
        mem.save_evolution_plan(
            _plan(f"p_{status}", "alpha bravo", "charlie delta", status=status),
        )
    cluster = find_pending_plan_cluster(
        mem, "alpha bravo\ncharlie delta", threshold=0.5,
    )
    assert sorted(p.plan_id for p in cluster) == [
        "p_approved", "p_implemented", "p_pending",
    ]


def test_find_pending_plan_cluster_empty_candidate_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """A candidate with no content tokens clusters with nothing."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_plan("p1", "alpha bravo", "charlie delta"))
    assert find_pending_plan_cluster(mem, "", threshold=0.5) == []


def test_find_pending_plan_cluster_skips_empty_token_plans(
    mem_fs: dict[str, str],
) -> None:
    """A pending plan with no content tokens is skipped without error."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_plan("p_empty", "is to", "the of"))
    mem.save_evolution_plan(_plan("p_match", "alpha bravo", "charlie delta"))
    cluster = find_pending_plan_cluster(
        mem, "alpha bravo\ncharlie delta", threshold=0.5,
    )
    assert [p.plan_id for p in cluster] == ["p_match"]


def test_find_pending_plan_cluster_looser_threshold_admits_more(
    mem_fs: dict[str, str],
) -> None:
    """A looser threshold pulls in variants the strict cutoff would miss.

    Candidate {alpha, bravo, charlie, delta, echo}; ``p1`` shares three
    with two unique tokens (Jaccard 3/7 ≈ 0.43). At the strict 0.5 cutoff
    it is excluded; at the looser 0.35 cutoff it joins the cluster.
    """
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_plan("p1", "alpha bravo charlie", "foxtrot golf"))
    candidate = "alpha bravo charlie\ndelta echo"
    assert find_pending_plan_cluster(mem, candidate, threshold=0.5) == []
    loose = find_pending_plan_cluster(mem, candidate, threshold=0.35)
    assert [p.plan_id for p in loose] == ["p1"]


def _cluster_plan(
    plan_id: str,
    created_at: str,
    unique: str,
    status: str = "pending_review",
) -> EvolutionPlan:
    """A near-restated plan: shared theme tokens plus one distinguishing word.

    Every such plan shares {alpha, bravo, charlie, delta, echo}; the *unique*
    token keeps the pair just apart, yielding a symmetric Jaccard of 5/7 ≈
    0.71 — comfortably above the 0.35 cluster cutoff so the trio clusters.
    """
    plan = _plan(plan_id, "alpha bravo charlie", f"delta echo {unique}", status)
    plan.created_at = created_at
    return plan


def test_sweep_clustered_pending_plans_keeps_newest(
    mem_fs: dict[str, str],
) -> None:
    """A 3-member cluster obsoletes the two older members and keeps the newest."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_cluster_plan("old1", "2025-01-01T00:00:00Z", "foxtrot"))
    mem.save_evolution_plan(_cluster_plan("old2", "2025-01-02T00:00:00Z", "golf"))
    mem.save_evolution_plan(_cluster_plan("new3", "2025-01-03T00:00:00Z", "hotel"))

    obsoleted = mem.sweep_clustered_pending_plans(
        threshold=0.35, min_cluster_size=2,
    )

    assert sorted(obsoleted) == ["old1", "old2"]
    assert {p.plan_id for p in mem.get_pending_plans()} == {"new3"}
    for pid in ("old1", "old2"):
        plan = mem.get_plan(pid)
        assert plan is not None
        assert plan.status == "obsolete"
        assert plan.obsolete_reason is not None


def test_sweep_clustered_pending_plans_ignores_non_pending(
    mem_fs: dict[str, str],
) -> None:
    """Plans whose status is not ``pending_review`` are never touched."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_cluster_plan("p_old", "2025-01-01T00:00:00Z", "foxtrot"))
    mem.save_evolution_plan(_cluster_plan("p_new", "2025-01-02T00:00:00Z", "golf"))
    # A merged plan that is textually newest of all and clusters by content,
    # yet must be ignored entirely because it is not pending_review.
    mem.save_evolution_plan(
        _cluster_plan("merged", "2025-12-31T00:00:00Z", "hotel", status="merged"),
    )

    obsoleted = mem.sweep_clustered_pending_plans(
        threshold=0.35, min_cluster_size=2,
    )

    # The merged plan is excluded from the cluster, so the newest *pending*
    # member (p_new) is kept and only the older pending member is retired.
    assert obsoleted == ["p_old"]
    merged = mem.get_plan("merged")
    assert merged is not None
    assert merged.status == "merged"
    assert mem.get_plan("p_new").status == "pending_review"  # type: ignore[union-attr]
    assert mem.get_plan("p_old").status == "obsolete"  # type: ignore[union-attr]


def test_sweep_clustered_pending_plans_ignores_approved_implemented(
    mem_fs: dict[str, str],
) -> None:
    """Approved/implemented cluster members are neither retired nor reported.

    Regression for proposal 111: collapsing ``find_pending_plan_cluster`` to the
    unconditional live-work corpus (``pending_review``/``approved``/
    ``implemented``) repoints this sweep's cluster read at approved/implemented
    plans too. ``mark_plan_obsolete`` refuses a non-``pending_review`` plan, so an
    approved/implemented member that landed as a retire target would be reported
    in the returned list without ever being flipped — a prune that never happened.
    Both non-pending members here are *older* than the newest pending plan, so
    pre-fix they were retire targets (``mark_plan_obsolete`` -> ``False``) yet
    still appended. The sweep must restrict the cluster to its pending members:
    only the older pending plan is retired and returned; the approved and
    implemented plans keep their status and never appear in the list.
    """
    mem = _make_memory(mem_fs)
    # Two live-but-non-pending members, both older than the newest pending plan.
    mem.save_evolution_plan(
        _cluster_plan("appr", "2025-01-01T00:00:00Z", "foxtrot", status="approved"),
    )
    mem.save_evolution_plan(
        _cluster_plan("impl", "2025-01-02T00:00:00Z", "golf", status="implemented"),
    )
    mem.save_evolution_plan(_cluster_plan("p_old", "2025-01-03T00:00:00Z", "hotel"))
    mem.save_evolution_plan(_cluster_plan("p_new", "2025-01-04T00:00:00Z", "india"))

    obsoleted = mem.sweep_clustered_pending_plans(
        threshold=0.35, min_cluster_size=2,
    )

    # Only the older *pending* member is retired and reported; the approved and
    # implemented members are untouched and absent from the list.
    assert obsoleted == ["p_old"]
    assert mem.get_plan("appr").status == "approved"  # type: ignore[union-attr]
    assert mem.get_plan("impl").status == "implemented"  # type: ignore[union-attr]
    assert mem.get_plan("p_new").status == "pending_review"  # type: ignore[union-attr]
    assert mem.get_plan("p_old").status == "obsolete"  # type: ignore[union-attr]


def test_sweep_clustered_pending_plans_no_cluster_no_writes(
    mem_fs: dict[str, str],
) -> None:
    """No qualifying cluster -> zero obsoletions and zero writes to disk."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(
        _plan("a", "Harden dashboard CSRF middleware ordering",
              "Security headers wrap every 401 from the public API surface"),
    )
    mem.save_evolution_plan(
        _plan("b", "Add prometheus latency histogram to router",
              "Instrument request duration buckets so operators chart tail latency"),
    )
    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    before = mem_fs[plans_path]

    obsoleted = mem.sweep_clustered_pending_plans(
        threshold=0.35, min_cluster_size=2,
    )

    assert obsoleted == []
    # The two distinct-theme plans are untouched and the file is byte-identical.
    assert mem_fs[plans_path] == before


def test_sweep_clustered_pending_plans_empty_backlog_no_writes(
    mem_fs: dict[str, str],
) -> None:
    """An empty or below-size backlog is a strict no-op (no file even created)."""
    mem = _make_memory(mem_fs)
    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")

    assert mem.sweep_clustered_pending_plans(threshold=0.35, min_cluster_size=2) == []
    assert plans_path not in mem_fs

    # A single pending plan cannot form a cluster of size 2.
    mem.save_evolution_plan(_cluster_plan("solo", "2025-01-01T00:00:00Z", "foxtrot"))
    before = mem_fs[plans_path]
    assert mem.sweep_clustered_pending_plans(threshold=0.35, min_cluster_size=2) == []
    assert mem_fs[plans_path] == before


def test_sweep_clustered_pending_plans_disabled_min_size(
    mem_fs: dict[str, str],
) -> None:
    """``min_cluster_size`` below 2 disables the sweep entirely (no writes)."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_cluster_plan("old1", "2025-01-01T00:00:00Z", "foxtrot"))
    mem.save_evolution_plan(_cluster_plan("new2", "2025-01-02T00:00:00Z", "golf"))
    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    before = mem_fs[plans_path]

    assert mem.sweep_clustered_pending_plans(threshold=0.35, min_cluster_size=1) == []
    assert mem.sweep_clustered_pending_plans(threshold=0.35, min_cluster_size=0) == []
    assert mem_fs[plans_path] == before


def test_sweep_clustered_pending_plans_idempotent(
    mem_fs: dict[str, str],
) -> None:
    """A second run over the post-sweep backlog obsoletes nothing."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_cluster_plan("old1", "2025-01-01T00:00:00Z", "foxtrot"))
    mem.save_evolution_plan(_cluster_plan("old2", "2025-01-02T00:00:00Z", "golf"))
    mem.save_evolution_plan(_cluster_plan("new3", "2025-01-03T00:00:00Z", "hotel"))

    first = mem.sweep_clustered_pending_plans(threshold=0.35, min_cluster_size=2)
    assert sorted(first) == ["old1", "old2"]

    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    after_first = mem_fs[plans_path]
    ledger_after_first = mem_fs[_LEVER_LEDGER]
    second = mem.sweep_clustered_pending_plans(threshold=0.35, min_cluster_size=2)
    assert second == []
    # Idempotent: the second pass writes nothing — no plan write, no lever row.
    assert mem_fs[plans_path] == after_first
    assert mem_fs[_LEVER_LEDGER] == ledger_after_first


def test_sweep_clustered_pending_plans_omits_refused_retire(
    mem_fs: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member approved after the pass snapshot is refused and not reported.

    The dashboard writes plan status concurrently, so a member can leave
    ``pending_review`` between the pass's ``get_pending_plans`` snapshot and
    the locked :meth:`Memory.mark_plan_obsolete` write. The lever refuses; the
    sweep must record that refusal and must not name the id as pruned (#4077).
    """
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_cluster_plan("old", "2025-01-01T00:00:00Z", "foxtrot"))
    mem.save_evolution_plan(_cluster_plan("new", "2025-01-02T00:00:00Z", "golf"))

    real_find = memory_module.find_pending_plan_cluster

    def racing_find(
        memory: Memory, text: str, threshold: float,
    ) -> list[EvolutionPlan]:
        cluster = real_find(memory, text, threshold)
        memory.update_plan_status("old", "approved")
        return cluster

    monkeypatch.setattr(memory_module, "find_pending_plan_cluster", racing_find)

    obsoleted = mem.sweep_clustered_pending_plans(
        threshold=0.35, min_cluster_size=2,
    )

    assert "old" not in obsoleted
    assert obsoleted == []
    plan = mem.get_plan("old")
    assert plan is not None
    assert plan.status == "approved"
    rows = [
        json.loads(line)
        for line in mem_fs[_LEVER_LEDGER].strip().splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["args"][0] == "old"
    assert rows[0]["result"] is False


def test_sweep_clustered_pending_plans_records_lever_row(
    mem_fs: dict[str, str],
) -> None:
    """Each genuine cluster prune appends one verified row to the injected ledger."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_cluster_plan("old1", "2025-01-01T00:00:00Z", "foxtrot"))
    mem.save_evolution_plan(_cluster_plan("old2", "2025-01-02T00:00:00Z", "golf"))
    mem.save_evolution_plan(_cluster_plan("new3", "2025-01-03T00:00:00Z", "hotel"))
    injected = str(FAKE_DIR / "injected")
    ledger = JsonlLeverAttemptsLedger(Path(injected))

    obsoleted = mem.sweep_clustered_pending_plans(
        threshold=0.35, min_cluster_size=2, lever_attempts=ledger,
    )

    assert sorted(obsoleted) == ["old1", "old2"]
    rows = ledger.load_all()
    assert sorted(row.args[0] for row in rows) == ["old1", "old2"]
    for row in rows:
        assert row.lever == "mark_plan_obsolete"
        assert row.verified is True
        assert row.result is True
    # The injected backend received the rows, not the JSONL default.
    assert _LEVER_LEDGER not in mem_fs


# ---------------------------------------------------------------------------
# Mechanical pending-plan restatement sweep — sweep_restatement_pending_plans
# ---------------------------------------------------------------------------

# A restatement pair: two plans whose combined title+rationale overlap well
# above the 0.5 default Jaccard cutoff (the pair the duplicate guard blocks).
_RESTATE_TITLE = "Escalate unlanded remediation to Thomas"
_RESTATE_RATIONALE = (
    "Deliver one merge-or-close message when a lineage stalls past threshold"
)
_RESTATE_TITLE_2 = "Escalate unlanded remediation to Thomas once"
_RESTATE_RATIONALE_2 = (
    "Deliver a single merge-or-close message when the lineage stalls past threshold"
)



def _restate_plan(plan_id: str, created_at: str, variant: int = 1) -> EvolutionPlan:
    if variant == 1:
        plan = _plan(plan_id, _RESTATE_TITLE, _RESTATE_RATIONALE)
    else:
        plan = _plan(plan_id, _RESTATE_TITLE_2, _RESTATE_RATIONALE_2)
    plan.created_at = created_at
    return plan


def test_sweep_restatement_retires_older_keeps_newest(
    mem_fs: dict[str, str],
) -> None:
    """A restatement pair retires the OLDER plan and keeps the newest."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_restate_plan("old", "2025-01-01T00:00:00Z", variant=1))
    mem.save_evolution_plan(_restate_plan("new", "2025-01-02T00:00:00Z", variant=2))

    obsoleted = mem.sweep_restatement_pending_plans(threshold=0.5)

    assert obsoleted == ["old"]
    assert {p.plan_id for p in mem.get_pending_plans()} == {"new"}
    old = mem.get_plan("old")
    assert old is not None
    assert old.status == "obsolete"
    assert old.obsolete_reason is not None


def test_sweep_restatement_leaves_unique_plan_untouched(
    mem_fs: dict[str, str],
) -> None:
    """A genuinely-unique pending plan is never retired by the sweep."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_restate_plan("old", "2025-01-01T00:00:00Z", variant=1))
    mem.save_evolution_plan(_restate_plan("new", "2025-01-02T00:00:00Z", variant=2))
    mem.save_evolution_plan(
        _plan(
            "unique",
            "Add prometheus latency histogram to the dashboard router",
            "Instrument request duration buckets so operators chart tail latency",
        ),
    )

    obsoleted = mem.sweep_restatement_pending_plans(threshold=0.5)

    assert obsoleted == ["old"]
    assert mem.get_plan("unique").status == "pending_review"  # type: ignore[union-attr]
    assert mem.get_plan("new").status == "pending_review"  # type: ignore[union-attr]


def test_sweep_restatement_empty_backlog_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """An empty pending backlog is a strict no-op returning []."""
    mem = _make_memory(mem_fs)

    assert mem.sweep_restatement_pending_plans(threshold=0.5) == []
    # No ledger row is written when nothing fires.
    assert _LEVER_LEDGER not in mem_fs


def test_sweep_restatement_idempotent(
    mem_fs: dict[str, str],
) -> None:
    """A second run over the drained backlog obsoletes nothing and writes nothing."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_restate_plan("old", "2025-01-01T00:00:00Z", variant=1))
    mem.save_evolution_plan(_restate_plan("new", "2025-01-02T00:00:00Z", variant=2))

    first = mem.sweep_restatement_pending_plans(threshold=0.5)
    assert first == ["old"]

    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    after_first_plans = mem_fs[plans_path]
    after_first_ledger = mem_fs[_LEVER_LEDGER]

    second = mem.sweep_restatement_pending_plans(threshold=0.5)
    assert second == []
    # Idempotent: neither the plans file nor the ledger grows.
    assert mem_fs[plans_path] == after_first_plans
    assert mem_fs[_LEVER_LEDGER] == after_first_ledger


def test_sweep_restatement_captures_one_ledger_row_per_retire(
    mem_fs: dict[str, str],
) -> None:
    """Each retire appends exactly one captured-return row (literal bool + reason)."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_restate_plan("old", "2025-01-01T00:00:00Z", variant=1))
    mem.save_evolution_plan(_restate_plan("new", "2025-01-02T00:00:00Z", variant=2))

    obsoleted = mem.sweep_restatement_pending_plans(threshold=0.5)
    assert obsoleted == ["old"]

    rows = [
        json.loads(line)
        for line in mem_fs[_LEVER_LEDGER].strip().splitlines()
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row["lever"] == "mark_plan_obsolete"
    assert row["result"] is True
    assert row["verified"] is True
    # The args carry both the target plan_id and the reason tag.
    assert row["args"][0] == "old"
    assert "restatement sweep" in row["args"][1].lower()


# ---------------------------------------------------------------------------
# Mechanical pending-plan expiry sweep — sweep_expired_pending_plans
# ---------------------------------------------------------------------------


def _aged_plan(
    plan_id: str,
    days_ago: float,
    status: str = "pending_review",
) -> EvolutionPlan:
    """A plan whose ``created_at`` is *days_ago* before now (UTC ISO)."""
    plan = _plan(plan_id, f"Plan {plan_id}", f"Rationale for {plan_id}", status)
    plan.created_at = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    return plan


def test_sweep_expired_retires_old_pending_keeps_young(
    mem_fs: dict[str, str],
) -> None:
    """Only pending_review plans older than the horizon are obsoleted."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_aged_plan("stale1", days_ago=30))
    mem.save_evolution_plan(_aged_plan("stale2", days_ago=100))
    mem.save_evolution_plan(_aged_plan("fresh", days_ago=5))

    obsoleted = mem.sweep_expired_pending_plans(max_age_days=21)

    assert set(obsoleted) == {"stale1", "stale2"}
    assert {p.plan_id for p in mem.get_pending_plans()} == {"fresh"}
    stale1 = mem.get_plan("stale1")
    assert stale1 is not None
    assert stale1.status == "obsolete"
    assert stale1.obsolete_reason is not None
    assert "expiry sweep" in stale1.obsolete_reason.lower()
    # An expiry is never recorded as a human rejection.
    assert stale1.rejection_reason is None


def test_sweep_expired_ignores_non_pending_plans(
    mem_fs: dict[str, str],
) -> None:
    """approved/implemented/rejected plans are never touched by the sweep."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_aged_plan("approved", days_ago=100, status="approved"))
    mem.save_evolution_plan(
        _aged_plan("implemented", days_ago=100, status="implemented"),
    )
    mem.save_evolution_plan(_aged_plan("rejected", days_ago=100, status="rejected"))

    obsoleted = mem.sweep_expired_pending_plans(max_age_days=21)

    assert obsoleted == []
    assert mem.get_plan("approved").status == "approved"  # type: ignore[union-attr]
    assert mem.get_plan("implemented").status == "implemented"  # type: ignore[union-attr]
    assert mem.get_plan("rejected").status == "rejected"  # type: ignore[union-attr]


def test_sweep_expired_disabled_when_max_age_non_positive(
    mem_fs: dict[str, str],
) -> None:
    """max_age_days <= 0 is a strict no-op: returns [], mutates nothing."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_aged_plan("stale", days_ago=100))
    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    before = mem_fs[plans_path]

    assert mem.sweep_expired_pending_plans(max_age_days=0) == []
    assert mem.sweep_expired_pending_plans(max_age_days=-5) == []

    # Nothing fired: neither the plans file nor the ledger changed.
    assert mem_fs[plans_path] == before
    assert _LEVER_LEDGER not in mem_fs
    assert mem.get_plan("stale").status == "pending_review"  # type: ignore[union-attr]


def test_sweep_expired_skips_unparseable_created_at(
    mem_fs: dict[str, str],
) -> None:
    """A plan with a missing/unparseable created_at is skipped, not crashed."""
    mem = _make_memory(mem_fs)
    missing = _plan("missing", "No timestamp", "Rationale")
    missing.created_at = ""
    mem.save_evolution_plan(missing)
    garbage = _plan("garbage", "Bad timestamp", "Rationale")
    garbage.created_at = "not-a-date"
    mem.save_evolution_plan(garbage)
    mem.save_evolution_plan(_aged_plan("stale", days_ago=100))

    obsoleted = mem.sweep_expired_pending_plans(max_age_days=21)

    # Only the parseable, aged plan is retired; the others survive untouched.
    assert obsoleted == ["stale"]
    assert mem.get_plan("missing").status == "pending_review"  # type: ignore[union-attr]
    assert mem.get_plan("garbage").status == "pending_review"  # type: ignore[union-attr]


def test_sweep_expired_idempotent(
    mem_fs: dict[str, str],
) -> None:
    """A second run over the drained backlog obsoletes nothing and writes nothing."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_aged_plan("stale", days_ago=100))

    first = mem.sweep_expired_pending_plans(max_age_days=21)
    assert first == ["stale"]

    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    after_first_plans = mem_fs[plans_path]
    after_first_ledger = mem_fs[_LEVER_LEDGER]

    second = mem.sweep_expired_pending_plans(max_age_days=21)
    assert second == []
    assert mem_fs[plans_path] == after_first_plans
    assert mem_fs[_LEVER_LEDGER] == after_first_ledger


def test_sweep_expired_captures_one_ledger_row_per_retire(
    mem_fs: dict[str, str],
) -> None:
    """Each retire appends exactly one captured-return row (literal bool + reason)."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_aged_plan("stale", days_ago=100))

    obsoleted = mem.sweep_expired_pending_plans(max_age_days=21)
    assert obsoleted == ["stale"]

    rows = [
        json.loads(line)
        for line in mem_fs[_LEVER_LEDGER].strip().splitlines()
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row["lever"] == "mark_plan_obsolete"
    assert row["result"] is True
    assert row["verified"] is True
    assert row["args"][0] == "stale"
    assert "expiry sweep" in row["args"][1].lower()


def test_sweep_expired_normalizes_naive_created_at(
    mem_fs: dict[str, str],
) -> None:
    """A tz-naive created_at is read as UTC — the naive fallback branch.

    Exercises ``created = parsed if parsed.tzinfo else
    parsed.replace(tzinfo=UTC)``: every helper-built plan stores an aware
    ISO string, so only a deliberately naive timestamp reaches the ``else``.
    Normalizing to UTC (rather than crashing on the aware/naive compare)
    must retire the aged plan and keep the recent one.
    """
    mem = _make_memory(mem_fs)
    old_naive = _plan("old-naive", "Naive old", "Rationale")
    old_naive.created_at = (
        (datetime.now(UTC) - timedelta(days=100)).replace(tzinfo=None).isoformat()
    )
    mem.save_evolution_plan(old_naive)
    young_naive = _plan("young-naive", "Naive young", "Rationale")
    young_naive.created_at = (
        (datetime.now(UTC) - timedelta(days=5)).replace(tzinfo=None).isoformat()
    )
    mem.save_evolution_plan(young_naive)

    obsoleted = mem.sweep_expired_pending_plans(max_age_days=21)

    assert obsoleted == ["old-naive"]
    assert mem.get_plan("old-naive").status == "obsolete"  # type: ignore[union-attr]
    assert mem.get_plan("young-naive").status == "pending_review"  # type: ignore[union-attr]
