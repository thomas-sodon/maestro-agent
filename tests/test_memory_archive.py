"""Plan index and tiered-memory archive rotation; split out of
``tests/test_memory.py``.
"""

from __future__ import annotations

import builtins as _builtins
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast, get_args
from unittest.mock import MagicMock, patch

import pytest

from maestro.cost_tracking import (
    CostSample,
    JsonlCostSampleStore,
    _clear_cost_rate_cache,
    append_cost_sample,
    build_cost_rates,
    cost_for_task,
)
from maestro.memory import (
    FINISHED_PUBLICATION_STATUSES,
    EvolutionPlan,
    EvolutionProposal,
    Memory,
    TaskRecord,
    _plan_row_archivable,
)
from maestro.sdk import IterationUsage
from maestro.types import ProposalStatus
from tests.test_memory import (
    FAKE_DIR,
    _make_memory,
    _make_proposal,
    _sample_plan,
)

# ---------------------------------------------------------------------------
# Plan index — fast get_plan path + invalidation on every write site
# ---------------------------------------------------------------------------


def test_get_plan_uses_indexed_seek(mem_fs: dict[str, str]) -> None:
    """An indexed lookup reads only the matching line, not the whole file.

    Builds the index once on the first ``get_plan`` call, then verifies that
    the second call reads exactly one line out of the file via ``readline``.
    """
    mem = _make_memory(mem_fs)
    for i in range(20):
        mem.save_evolution_plan(_sample_plan(plan_id=f"ep{i:02d}"))

    target = mem.get_plan("ep17")
    assert target is not None
    assert target.plan_id == "ep17"
    assert mem._plan_index is not None
    assert "ep17" in mem._plan_index

    # Spy on readline calls to confirm the second lookup reads only one
    # line — the seek+readline fast path — instead of streaming all 20.
    real_open = _builtins.open
    readline_calls: list[str] = []

    def spy_open(*args: Any, **kwargs: Any) -> Any:
        handle = real_open(*args, **kwargs)
        original_readline = handle.readline

        def counting_readline(*a: Any, **kw: Any) -> Any:
            line = original_readline(*a, **kw)
            readline_calls.append(line)
            return line

        handle.readline = counting_readline  # type: ignore[method-assign]
        return handle

    with patch("builtins.open", side_effect=spy_open):
        again = mem.get_plan("ep17")
    assert again is not None
    assert again.plan_id == "ep17"
    # Exactly one non-empty readline (the targeted line).  An empty
    # readline at EOF is acceptable but a second non-empty readline
    # would mean we are scanning past the target.
    non_empty = [line for line in readline_calls if line.strip()]
    assert len(non_empty) == 1


def test_get_plan_returns_none_for_missing_id(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan(plan_id="ep1"))
    assert mem.get_plan("does-not-exist") is None
    assert mem.get_plan("") is None


def test_get_plan_returns_none_when_file_missing(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    assert mem.get_plan("anything") is None


def test_load_all_plans_skips_row_failing_construction(mem_fs: dict[str, str]) -> None:
    """A valid-JSON plan row missing required fields is skipped, not fatal.

    Field-filtering leaves ``EvolutionPlan(**filtered)`` short of its required
    args, so ``_parse_plan_line`` catches the ``TypeError`` and returns ``None``;
    the row is dropped and any well-formed sibling survives.
    """
    mem = _make_memory(mem_fs)
    valid = asdict(_sample_plan(plan_id="ep_ok"))
    incomplete = {"plan_id": "ep_bad"}  # missing title/stage/category/... required
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = (
        json.dumps(incomplete) + "\n" + json.dumps(valid) + "\n"
    )

    plans = mem._load_all_plans()
    assert [p.plan_id for p in plans] == ["ep_ok"]


def test_save_evolution_plan_invalidates_index(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan(plan_id="ep1"))
    assert mem.get_plan("ep1") is not None
    assert mem._plan_index is not None
    mem.save_evolution_plan(_sample_plan(plan_id="ep2"))
    assert mem._plan_index is None
    # Next get_plan rebuilds against the new state.
    found = mem.get_plan("ep2")
    assert found is not None and found.plan_id == "ep2"


def test_update_plan_status_invalidates_index(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan(plan_id="ep1"))
    mem.get_plan("ep1")  # warm the cache
    assert mem._plan_index is not None
    mem.update_plan_status("ep1", "implemented")
    assert mem._plan_index is None
    found = mem.get_plan("ep1")
    assert found is not None and found.status == "implemented"


def test_update_plan_status_missing_id_does_not_invalidate(
    mem_fs: dict[str, str],
) -> None:
    """A missing-id update writes nothing, so it must not touch the cache."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan(plan_id="ep1"))
    mem.get_plan("ep1")
    cached = mem._plan_index
    assert cached is not None
    mem.update_plan_status("does-not-exist", "implemented")
    # No write occurred → cache is preserved (still the same dict).
    assert mem._plan_index is cached


def test_mark_rejection_learned_invalidates_index(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan(plan_id="ep1"))
    mem.update_plan_status("ep1", "rejected")
    mem.get_plan("ep1")
    assert mem._plan_index is not None
    mem.mark_rejection_learned("ep1")
    assert mem._plan_index is None


# ---------------------------------------------------------------------------
# Archive rotation
# ---------------------------------------------------------------------------


def _task_at(task_id: str, days_ago: float) -> TaskRecord:
    """Build a ``TaskRecord`` whose timestamp is *days_ago* days before now."""
    ts = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    return TaskRecord(
        task_id=task_id,
        goal="g",
        plan=[],
        outcome="success",
        result_summary="s",
        errors=[],
        duration_secs=0.0,
        timestamp=ts,
    )


def test_archive_disabled_is_noop(mem_fs: dict[str, str]) -> None:
    """``archive(0)`` writes nothing even when records pre-date any threshold."""
    mem = _make_memory(mem_fs)
    mem.record_task(_task_at("t1", days_ago=1000))
    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    archive_path = str(FAKE_DIR / "task_history.jsonl.archive.jsonl")
    before = mem_fs[tasks_path]

    mem.archive(0)

    assert mem_fs[tasks_path] == before
    assert archive_path not in mem_fs


def test_archive_rotation_splits_hot_and_cold(mem_fs: dict[str, str]) -> None:
    """Records older than the threshold land in the cold sibling file."""
    mem = _make_memory(mem_fs)
    # 5 recent (≤ 7 days), 5 ancient (60 days) — interleave to verify
    # cold-file order matches input order regardless of position.
    spec = [(0, 1), (1, 60), (2, 1), (3, 60), (4, 1), (5, 60), (6, 1), (7, 60), (8, 1), (9, 60)]
    for idx, days in spec:
        mem.record_task(_task_at(f"t{idx}", days_ago=float(days)))

    mem.archive(threshold_days=7)

    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    archive_path = str(FAKE_DIR / "task_history.jsonl.archive.jsonl")
    assert tasks_path in mem_fs
    assert archive_path in mem_fs

    hot_ids = [json.loads(line)["task_id"] for line in mem_fs[tasks_path].splitlines() if line]
    cold_ids = [json.loads(line)["task_id"] for line in mem_fs[archive_path].splitlines() if line]

    assert sorted(hot_ids) == ["t0", "t2", "t4", "t6", "t8"]
    # Cold preserves insertion order from the input file.
    assert cold_ids == ["t1", "t3", "t5", "t7", "t9"]


def test_archive_unparseable_timestamp_stays_hot(mem_fs: dict[str, str]) -> None:
    """A row with a malformed timestamp is never archived."""
    mem = _make_memory(mem_fs)
    mem.record_task(_task_at("recent", days_ago=1))
    # Inject a record with an unparseable timestamp directly into the
    # JSONL file — record_task does not validate timestamp content.
    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    bad = TaskRecord(
        task_id="malformed",
        goal="g", plan=[], outcome="success", result_summary="s",
        errors=[], duration_secs=0.0, timestamp="not-a-date",
    )
    mem_fs[tasks_path] = mem_fs[tasks_path] + json.dumps(asdict(bad)) + "\n"

    mem.archive(threshold_days=7)

    hot_ids = [json.loads(line)["task_id"] for line in mem_fs[tasks_path].splitlines() if line]
    assert "malformed" in hot_ids
    assert "recent" in hot_ids


def test_archive_appends_to_existing_archive(mem_fs: dict[str, str]) -> None:
    """A second rotation appends to ``<file>.archive.jsonl`` rather than overwriting."""
    mem = _make_memory(mem_fs)
    mem.record_task(_task_at("old1", days_ago=60))
    mem.archive(threshold_days=7)

    archive_path = str(FAKE_DIR / "task_history.jsonl.archive.jsonl")
    assert archive_path in mem_fs
    first_size = len(mem_fs[archive_path].splitlines())

    mem.record_task(_task_at("old2", days_ago=60))
    mem.archive(threshold_days=7)

    second_lines = mem_fs[archive_path].splitlines()
    assert len(second_lines) == first_size + 1
    last_id = json.loads(second_lines[-1])["task_id"]
    assert last_id == "old2"


def test_archive_rotation_invalidates_plan_index(mem_fs: dict[str, str]) -> None:
    """After rotating ``_plans_path``, the next ``get_plan`` rebuilds the index."""
    mem = _make_memory(mem_fs)

    def plan_at(plan_id: str, days_ago: float) -> EvolutionPlan:
        ts = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
        return EvolutionPlan(
            plan_id=plan_id,
            title="t", category="capability",
            rationale="r", proposed_changes=[], files_affected=[],
            expected_impact="", created_at=ts,
        )

    mem.save_evolution_plan(plan_at("old", days_ago=60))
    mem.save_evolution_plan(plan_at("recent", days_ago=1))
    # Only finished history rotates, so the old row must carry a terminal
    # status for this rotation to happen at all.
    mem.update_plan_status("old", "merged")
    mem.get_plan("recent")
    assert mem._plan_index is not None

    mem.archive(threshold_days=7)
    assert mem._plan_index is None
    # Recent plan still found via the rebuilt index.
    found = mem.get_plan("recent")
    assert found is not None and found.plan_id == "recent"
    # Old plan is gone from the hot file.
    assert mem.get_plan("old") is None


def _plan_at(plan_id: str, days_ago: float, status: str = "pending_review",
             parent_plan_id: str | None = None) -> EvolutionPlan:
    """An ``EvolutionPlan`` *days_ago* days old with an explicit lifecycle status."""
    ts = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    return EvolutionPlan(
        plan_id=plan_id,
        title="t", category="capability",
        rationale="r", proposed_changes=[], files_affected=[],
        expected_impact="", created_at=ts,
        status=cast(Any, status),
        parent_plan_id=parent_plan_id,
    )


def _plan_ids(text: str) -> list[str]:
    return [json.loads(line)["plan_id"] for line in text.splitlines() if line.strip()]


def test_archive_keeps_unfinished_plans_hot(mem_fs: dict[str, str]) -> None:
    """A plan the loop or a human still owes an action on never rotates (#3881).

    ``evolution_plans.jsonl`` is the authoritative store for live plan work, and
    no hot path reads the cold sibling — so shedding an old-but-unfinished row
    silently ends its lifecycle. Age alone must not do that.
    """
    mem = _make_memory(mem_fs)
    unfinished = ["pending_review", "approved", "failed", "blocked", "sliced",
                  "implemented"]
    for status in unfinished:
        mem.save_evolution_plan(_plan_at(status, days_ago=30, status=status))

    mem.archive(threshold_days=8)

    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    archive_path = plans_path + ".archive.jsonl"
    hot_ids = {p.plan_id for p in mem.get_all_plans()}
    assert hot_ids == set(unfinished)
    # Nothing rotated at all, so the cold sibling was never created.
    assert archive_path not in mem_fs
    assert set(_plan_ids(mem_fs[plans_path])) == set(unfinished)


def test_archive_keeps_unrecognised_plan_status_hot(mem_fs: dict[str, str]) -> None:
    """An unknown status reads as unfinished — the set is an allowlist."""
    mem = _make_memory(mem_fs)
    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    mem.save_evolution_plan(_plan_at("future", days_ago=30, status="merged"))
    mem_fs[plans_path] = mem_fs[plans_path].replace('"merged"', '"some_future_status"')

    mem.archive(threshold_days=8)

    assert _plan_ids(mem_fs[plans_path]) == ["future"]
    assert plans_path + ".archive.jsonl" not in mem_fs


def test_archive_keeps_slices_of_unfinished_proposal_hot(
    mem_fs: dict[str, str],
) -> None:
    """A terminal slice of a live proposal stays hot so the chain reconstructs."""
    mem = _make_memory(mem_fs)
    mem.write_evolution_proposal(
        EvolutionProposal(proposal_id="prop_live", title="t", goal="g",
                          status="rolling_out")
    )
    mem.write_evolution_proposal(
        EvolutionProposal(proposal_id="prop_done", title="t", goal="g",
                          status="complete")
    )
    mem.save_evolution_plan(
        _plan_at("live_slice", days_ago=30, status="merged",
                 parent_plan_id="prop_live")
    )
    mem.save_evolution_plan(
        _plan_at("done_slice", days_ago=30, status="merged",
                 parent_plan_id="prop_done")
    )

    mem.archive(threshold_days=8)

    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    archive_path = plans_path + ".archive.jsonl"
    assert _plan_ids(mem_fs[plans_path]) == ["live_slice"]
    assert _plan_ids(mem_fs[archive_path]) == ["done_slice"]
    # The live proposal's ordered chain is still reconstructible.
    assert [p.plan_id for p in mem.get_slices_for_proposal("prop_live")] == [
        "live_slice"
    ]


def test_archive_keeps_slices_of_unrecognised_proposal_status_hot(
    mem_fs: dict[str, str],
) -> None:
    """An unknown proposal status reads as unfinished — its slices stay hot."""
    mem = _make_memory(mem_fs)
    mem.write_evolution_proposal(
        EvolutionProposal(proposal_id="prop_new", title="t", goal="g",
                          status=cast(Any, "some_future_status"))
    )
    mem.save_evolution_plan(
        _plan_at("slice", days_ago=30, status="merged",
                 parent_plan_id="prop_new")
    )

    mem.archive(threshold_days=8)

    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    assert _plan_ids(mem_fs[plans_path]) == ["slice"]
    assert plans_path + ".archive.jsonl" not in mem_fs


def test_archive_rotates_failed_plan_past_retry_budget(
    mem_fs: dict[str, str],
) -> None:
    """A ``failed`` plan rotates only once its retry budget is provably spent.

    ``_implement_approved_plans`` never re-admits a plan whose
    ``failure_count`` reached ``max_implementation_attempts``, so such a row is
    terminal in practice; without this arm exhausted failures would accumulate
    in the hot file forever.
    """
    mem = _make_memory(mem_fs)
    spent = _plan_at("spent", days_ago=30, status="failed")
    spent.failure_count = 3
    retrying = _plan_at("retrying", days_ago=30, status="failed")
    retrying.failure_count = 2
    mem.save_evolution_plan(spent)
    mem.save_evolution_plan(retrying)

    mem.archive(threshold_days=8, retry_budget=3)

    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    assert _plan_ids(mem_fs[plans_path]) == ["retrying"]
    assert _plan_ids(mem_fs[plans_path + ".archive.jsonl"]) == ["spent"]


def test_archive_without_retry_budget_keeps_failed_plans_hot(
    mem_fs: dict[str, str],
) -> None:
    """An un-supplied budget reads as unknown, so no failed row is shed."""
    mem = _make_memory(mem_fs)
    spent = _plan_at("spent", days_ago=30, status="failed")
    spent.failure_count = 99
    mem.save_evolution_plan(spent)

    mem.archive(threshold_days=8)

    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    assert _plan_ids(mem_fs[plans_path]) == ["spent"]
    assert plans_path + ".archive.jsonl" not in mem_fs


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"status": "merged"}, True),
        # ``rejected`` is learning-conditional: unlearned rows stay hot.
        ({"status": "rejected"}, False),
        ({"status": "rejected", "rejection_learned": False}, False),
        ({"status": "rejected", "rejection_learned": True}, True),
        ({"status": "obsolete"}, True),
        ({"status": "approved"}, False),
        ({"status": "unknown_future"}, False),
        ({}, False),
        ({"status": 7}, False),
        # ``failed`` is budget-conditional: spent rotates, unspent does not.
        ({"status": "failed", "failure_count": 3}, True),
        ({"status": "failed", "failure_count": 2}, False),
        # A ``bool`` is an ``int`` subclass but never a real count.
        ({"status": "failed", "failure_count": True}, False),
        ({"status": "failed", "failure_count": "3"}, False),
        ({"status": "failed"}, False),
        # A terminal slice of a still-open proposal stays hot.
        ({"status": "merged", "parent_plan_id": "open"}, False),
        ({"status": "merged", "parent_plan_id": "other"}, True),
        ({"status": "merged", "parent_plan_id": 5}, True),
    ],
)
def test_plan_row_archivable_predicate(
    row: dict[str, object], expected: bool
) -> None:
    """The pure predicate over one raw plan row, branch by branch."""
    assert _plan_row_archivable(row, {"open"}, 3) is expected


def test_plan_row_archivable_unknown_budget_holds_failed_row() -> None:
    """``retry_budget=None`` means "budget unknown", so no failed row rotates."""
    assert _plan_row_archivable({"status": "failed", "failure_count": 99}, set()) is False


def test_archive_rotates_finished_plan_history(mem_fs: dict[str, str]) -> None:
    """Finished, unreferenced plans still rotate — the fix does not stop rotation."""
    mem = _make_memory(mem_fs)
    for status in ("merged", "rejected", "obsolete"):
        plan = _plan_at(status, days_ago=30, status=status)
        plan.rejection_learned = True
        mem.save_evolution_plan(plan)
    mem.save_evolution_plan(_plan_at("fresh", days_ago=1, status="merged"))

    mem.archive(threshold_days=8)

    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    archive_path = plans_path + ".archive.jsonl"
    assert _plan_ids(mem_fs[plans_path]) == ["fresh"]
    assert _plan_ids(mem_fs[archive_path]) == ["merged", "rejected", "obsolete"]


def test_plan_row_archivable_open_pr_holds() -> None:
    """A retired row whose PR is open — or whose PR state is unknown — stays hot."""
    obsolete = {"status": "obsolete", "plan_id": "p1", "pr_url": "u"}
    rejected = {"status": "rejected", "plan_id": "p1", "pr_url": "u",
                "rejection_learned": True}
    assert _plan_row_archivable(obsolete, set(), 3, {"p1"}) is False
    assert _plan_row_archivable(rejected, set(), 3, {"p1"}) is False
    assert _plan_row_archivable(obsolete, set(), 3, {"other"}) is True
    assert _plan_row_archivable(obsolete, set(), 3, None) is False
    assert _plan_row_archivable(rejected, set(), 3, None) is False
    assert _plan_row_archivable({"status": "obsolete"}, set(), 3, None) is True
    # A merged row never holds on PR state.
    merged = {"status": "merged", "plan_id": "p1", "pr_url": "u"}
    assert _plan_row_archivable(merged, set(), 3, None) is True


def test_archive_keeps_obsolete_plan_with_open_pr_hot(
    mem_fs: dict[str, str],
) -> None:
    """An obsolete plan whose PR Tend still services is not shed (#4114)."""
    mem = _make_memory(mem_fs)
    for pid in ("open_pr", "closed_pr"):
        plan = _plan_at(pid, days_ago=40, status="obsolete")
        plan.pr_url = f"https://example.invalid/{pid}"
        mem.save_evolution_plan(plan)

    mem.archive(threshold_days=8, retry_budget=3, open_pr_plan_ids={"open_pr"})

    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    assert _plan_ids(mem_fs[plans_path]) == ["open_pr"]
    assert _plan_ids(mem_fs[plans_path + ".archive.jsonl"]) == ["closed_pr"]


def test_archive_unknown_pr_state_keeps_pr_backed_retired_rows_hot(
    mem_fs: dict[str, str],
) -> None:
    """``open_pr_plan_ids=None`` never sheds a retired row that carries a PR."""
    mem = _make_memory(mem_fs)
    for pid, status in (("obs", "obsolete"), ("rej", "rejected")):
        plan = _plan_at(pid, days_ago=40, status=status)
        plan.pr_url = "https://example.invalid/pr"
        plan.rejection_learned = True
        mem.save_evolution_plan(plan)

    mem.archive(threshold_days=8, retry_budget=3)

    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    assert _plan_ids(mem_fs[plans_path]) == ["obs", "rej"]
    assert plans_path + ".archive.jsonl" not in mem_fs


def test_archive_keeps_unlearned_rejected_plan_hot(mem_fs: dict[str, str]) -> None:
    """A legacy rejected row never learned from stays hot for the learner."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_plan_at("unlearned", days_ago=40, status="rejected"))

    mem.archive(threshold_days=8, retry_budget=3)

    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    assert _plan_ids(mem_fs[plans_path]) == ["unlearned"]
    assert plans_path + ".archive.jsonl" not in mem_fs


def test_archive_keeps_task_row_cited_by_live_plan_hot(
    mem_fs: dict[str, str],
) -> None:
    """A task a hot plan cites as ``source_task_id`` stays resolvable (#4114)."""
    mem = _make_memory(mem_fs)
    for tid in ("t_approved", "t_retry", "t_merged", "t_spent", "t_free"):
        mem.record_task(_task_at(tid, days_ago=25))
    cites = [
        ("p_approved", "approved", "t_approved", 0),
        ("p_retry", "failed", "t_retry", 1),
        ("p_merged", "merged", "t_merged", 0),
        ("p_spent", "failed", "t_spent", 3),
    ]
    for pid, status, tid, failures in cites:
        plan = _plan_at(pid, days_ago=30, status=status)
        plan.source_task_id = tid
        plan.failure_count = failures
        mem.save_evolution_plan(plan)
    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    # Blank and non-dict plan lines are skipped by the citation scan.
    mem_fs[plans_path] += "\nnot json\n[1]\n"

    mem.archive(threshold_days=8, retry_budget=3)

    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    hot = [json.loads(line)["task_id"] for line in mem_fs[tasks_path].splitlines()]
    assert hot == ["t_approved", "t_retry"]
    approved = mem.get_task_record("t_approved")
    assert approved is not None
    assert approved.task_id == "t_approved"


@pytest.mark.parametrize(
    ("status", "stays_hot"),
    [
        ("pending_approval", True),
        ("approved", True),
        ("executing", True),
        ("published", False),
        ("rejected", False),
        ("failed", False),
    ],
)
def test_archive_keeps_task_row_cited_by_open_publication_hot(
    mem_fs: dict[str, str], status: ProposalStatus, stays_hot: bool,
) -> None:
    """An unfinished publication pins its source task hot (#4326).

    No ``evolution_plans.jsonl`` exists, so the hold must not hinge on it.
    """
    mem = _make_memory(mem_fs)
    mem.record_task(_task_at("t_pub", days_ago=60))
    proposal = _make_proposal(task_id="t_pub")
    proposal.status = status
    mem.write_publication_proposal(proposal)

    mem.archive(threshold_days=30)

    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    hot = [json.loads(line)["task_id"] for line in mem_fs.get(tasks_path, "").splitlines()]
    record = mem.get_task("t_pub")
    if stays_hot:
        assert hot == ["t_pub"]
        assert record is not None
        assert tasks_path + ".archive.jsonl" not in mem_fs
    else:
        assert hot == []
        assert record is None
        assert tasks_path + ".archive.jsonl" in mem_fs


def test_archive_publication_and_plan_holds_combine(
    mem_fs: dict[str, str],
) -> None:
    """Plan and publication holds apply together; an empty task_id holds nothing."""
    mem = _make_memory(mem_fs)
    for tid in ("t_plan", "t_pub", "t_free"):
        mem.record_task(_task_at(tid, days_ago=60))
    plan = _plan_at("p_live", days_ago=1, status="approved")
    plan.source_task_id = "t_plan"
    mem.save_evolution_plan(plan)
    mem.write_publication_proposal(_make_proposal(proposal_id="pub1", task_id="t_pub"))
    mem.write_publication_proposal(_make_proposal(proposal_id="pub2", task_id=""))

    mem.archive(threshold_days=30)

    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    hot = [json.loads(line)["task_id"] for line in mem_fs[tasks_path].splitlines()]
    assert hot == ["t_plan", "t_pub"]


@pytest.mark.parametrize(
    ("field", "value", "stays_hot"),
    [
        ("task_id", ["t_pub"], False),
        ("status", ["published"], True),
    ],
)
def test_archive_publication_hold_tolerates_malformed_rows(
    mem_fs: dict[str, str], field: str, value: object, stays_hot: bool,
) -> None:
    """A non-str task_id holds nothing; a non-str status fails safe and holds."""
    mem = _make_memory(mem_fs)
    mem.record_task(_task_at("t_pub", days_ago=60))
    proposal = _make_proposal(task_id="t_pub")
    proposal.status = "published"
    setattr(proposal, field, value)
    mem.write_publication_proposal(proposal)

    mem.archive(threshold_days=30)

    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    hot = [json.loads(line)["task_id"] for line in mem_fs.get(tasks_path, "").splitlines()]
    if stays_hot:
        assert hot == ["t_pub"]
    else:
        assert hot == []


def test_finished_publication_statuses_are_proposal_statuses() -> None:
    """A renamed ``ProposalStatus`` must not silently read as unfinished."""
    assert FINISHED_PUBLICATION_STATUSES.issubset(get_args(ProposalStatus))


def _archive_with_citing_plan(
    mem_fs: dict[str, str], plan_row: dict[str, object],
) -> tuple[Memory, str]:
    """Seed a 60-day-old ``t_src`` cited by *plan_row*, then archive."""
    mem = _make_memory(mem_fs)
    mem.record_task(_task_at("t_src", days_ago=60))
    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    mem_fs[plans_path] = json.dumps({**plan_row, "source_task_id": "t_src"}) + "\n"
    mem.archive(threshold_days=22, retry_budget=3)
    return mem, plans_path


def _old_learned_rejection(reviewed_days_ago: float) -> dict[str, object]:
    plan = _plan_at("p_rej", days_ago=60, status="rejected")
    plan.rejection_learned = True
    plan.reviewed_at = (
        datetime.now(UTC) - timedelta(days=reviewed_days_ago)
    ).isoformat()
    return asdict(plan)


@pytest.mark.parametrize(
    "plan_row",
    [
        pytest.param(asdict(_plan_at("p_new", days_ago=1, status="merged")),
                     id="recent-merged"),
        pytest.param({"plan_id": "p_undated", "status": "merged",
                      "created_at": "not-a-date"}, id="unparseable-created-at"),
        pytest.param({"plan_id": "p_missing", "status": "merged"},
                     id="missing-created-at"),
        pytest.param(_old_learned_rejection(1), id="recently-reviewed-rejection"),
    ],
)
def test_archive_holds_source_task_of_date_retained_plan(
    mem_fs: dict[str, str], plan_row: dict[str, object],
) -> None:
    """A plan kept hot by a date branch also holds its source task (#4301)."""
    mem, plans_path = _archive_with_citing_plan(mem_fs, plan_row)

    assert _plan_ids(mem_fs[plans_path]) == [plan_row["plan_id"]]
    record = mem.get_task_record("t_src")
    assert record is not None
    assert record.task_id == "t_src"


def test_archive_rotates_source_task_of_archivable_plan(
    mem_fs: dict[str, str],
) -> None:
    """An old task cited only by an old, archivable plan goes cold with it."""
    mem, plans_path = _archive_with_citing_plan(mem_fs, _old_learned_rejection(40))

    assert mem_fs[plans_path] == ""
    assert _plan_ids(mem_fs[plans_path + ".archive.jsonl"]) == ["p_rej"]
    assert mem.get_task_record("t_src") is None


def test_archive_dates_rejected_plan_by_reviewed_at(mem_fs: dict[str, str]) -> None:
    """A rejected plan created long ago but rejected recently stays hot.

    The rejection cooldown reads the hot tier dated by ``reviewed_at``, so a
    long-lived PR closed yesterday must not be shed by its ``created_at``.
    """
    mem = _make_memory(mem_fs)
    recent = _plan_at("recent", days_ago=30, status="rejected")
    # Learned, so only the ``reviewed_at`` dating (not the unlearned-rejection
    # hold) keeps it hot.
    recent.rejection_learned = True
    recent.reviewed_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    stale = _plan_at("stale", days_ago=30, status="rejected")
    stale.rejection_learned = True
    stale.reviewed_at = (datetime.now(UTC) - timedelta(days=20)).isoformat()
    mem.save_evolution_plan(recent)
    mem.save_evolution_plan(stale)

    mem.archive(threshold_days=8)

    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    assert _plan_ids(mem_fs[plans_path]) == ["recent"]
    assert _plan_ids(mem_fs[plans_path + ".archive.jsonl"]) == ["stale"]


def test_archive_no_cold_records_skips_rewrite(mem_fs: dict[str, str]) -> None:
    """When no record is older than the threshold, the file is not rewritten."""
    mem = _make_memory(mem_fs)
    mem.record_task(_task_at("recent", days_ago=1))
    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    before = mem_fs[tasks_path]

    mem.archive(threshold_days=7)

    assert mem_fs[tasks_path] == before
    archive_path = str(FAKE_DIR / "task_history.jsonl.archive.jsonl")
    assert archive_path not in mem_fs


def test_archive_missing_files_is_noop(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    # Neither tasks nor plans exist yet — must not raise.
    mem.archive(threshold_days=7)


def test_archive_skips_non_dict_lines(mem_fs: dict[str, str]) -> None:
    """A JSON line that parses to a non-dict (legacy) is treated as hot."""
    mem = _make_memory(mem_fs)
    mem.record_task(_task_at("recent", days_ago=1))
    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    # Inject a line that is valid JSON but not a dict.
    mem_fs[tasks_path] = mem_fs[tasks_path] + '"just a string"\n'

    mem.archive(threshold_days=7)

    # The non-dict line stayed in the hot file.
    assert '"just a string"' in mem_fs[tasks_path]


def test_archive_corrupted_line_stays_hot(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.record_task(_task_at("recent", days_ago=1))
    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    mem_fs[tasks_path] = mem_fs[tasks_path] + "{not json\n"

    mem.archive(threshold_days=7)

    assert "{not json" in mem_fs[tasks_path]


def test_archive_skips_blank_lines(mem_fs: dict[str, str]) -> None:
    """Blank lines in the input file are skipped without contributing rows."""
    mem = _make_memory(mem_fs)
    mem.record_task(_task_at("recent", days_ago=1))
    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    # Inject a blank line.
    mem_fs[tasks_path] = mem_fs[tasks_path] + "\n\n"

    mem.archive(threshold_days=7)

    # Only the recent record remains; blank lines were dropped.
    hot_ids = [json.loads(line)["task_id"] for line in mem_fs[tasks_path].splitlines() if line.strip()]
    assert hot_ids == ["recent"]


def test_archive_hot_rewritten_before_cold_appended(mem_fs: dict[str, str]) -> None:
    """Crash in ``atomic_write`` leaves both files untouched (cold append never starts)."""
    mem = _make_memory(mem_fs)
    mem.record_task(_task_at("recent", days_ago=1))
    mem.record_task(_task_at("ancient", days_ago=60))
    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    archive_path = str(FAKE_DIR / "task_history.jsonl.archive.jsonl")
    before = mem_fs[tasks_path]

    with (
        patch(
            "maestro.memory.atomic_write",
            MagicMock(side_effect=OSError("simulated crash")),
        ),
        pytest.raises(OSError, match="simulated crash"),
    ):
        mem.archive(threshold_days=7)

    # atomic_write raised before cold append ran, so the archive file
    # was never created.
    assert archive_path not in mem_fs
    # atomic_write was mocked out — the hot file is byte-equal to its
    # pre-call contents.
    assert mem_fs[tasks_path] == before


def test_archive_no_cold_duplication_after_simulated_crash(mem_fs: dict[str, str]) -> None:
    """Re-running ``archive`` after a crash does not duplicate cold records."""
    mem = _make_memory(mem_fs)
    mem.record_task(_task_at("old1", days_ago=60))
    mem.record_task(_task_at("old2", days_ago=60))
    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    archive_path = str(FAKE_DIR / "task_history.jsonl.archive.jsonl")

    # Pre-rotation snapshot of the hot file so we can re-inject it later
    # (simulating the original bug's "crash left hot intact after cold
    # append" state).
    hot_before = mem_fs[tasks_path]
    mem.archive(threshold_days=7)
    cold_after_first = mem_fs[archive_path]

    # Simulate the legacy ordering's failure mode: the same ancient
    # records reappear in the hot file (as if cold had been appended but
    # the hot rewrite never committed).
    mem_fs[tasks_path] = hot_before

    with (
        patch(
            "maestro.memory.atomic_write",
            MagicMock(side_effect=OSError("simulated crash")),
        ),
        pytest.raises(OSError, match="simulated crash"),
    ):
        mem.archive(threshold_days=7)

    # Cold archive is byte-equal to the captured content — hot-first
    # ordering means the second archive crashed before any cold append
    # could duplicate the batch.
    assert mem_fs[archive_path] == cold_after_first


def _cost_sample_at(ts: datetime, *, cost: float) -> CostSample:
    """A minimal :class:`CostSample` stamped at *ts* (ISO ``timestamp`` key)."""
    return CostSample(
        timestamp=ts.isoformat(),
        cost_usd=cost,
        input_tokens=1000,
        output_tokens=200,
        cache_read_tokens=500,
        cache_creation_tokens=100,
        caller="test_caller",
        duration_ms=1500,
    )


def _append_usage(
    mem_fs: dict[str, str], path: Path, usage: IterationUsage,
) -> None:
    """Append one ``usage.jsonl`` row (``IterationUsage.to_dict`` shape)."""
    mem_fs[str(path)] = (
        mem_fs.get(str(path), "") + json.dumps(usage.to_dict()) + "\n"
    )


def test_archive_rotates_telemetry_sidecars_preserving_hot_window(
    mem_fs: dict[str, str],
) -> None:
    """``cost_samples.jsonl`` / ``usage.jsonl`` split into hot/cold tiers while
    every hot-window read stays byte-identical across the split (#2571).

    Cost-sample rows carry an ISO ``timestamp``; usage rows carry
    ``started_at`` (no ``timestamp``), so both bucket only once
    ``_record_timestamp`` learns the ``started_at`` key. A usage row with an
    unparseable ``started_at`` must never be archived.
    """
    mem = _make_memory(mem_fs)
    now = datetime.now(UTC)
    recent = now - timedelta(minutes=10)  # inside the 1h burn-rate window
    ancient = now - timedelta(days=60)  # outside the window and past threshold

    cost_path = FAKE_DIR / "cost_samples.jsonl"
    usage_path = FAKE_DIR / "usage.jsonl"

    # Seed cost_samples.jsonl ancient-first so append order stays
    # timestamp-monotonic (the burn-rate reverse-walk relies on it).
    append_cost_sample(cost_path, _cost_sample_at(ancient, cost=5.0))
    append_cost_sample(cost_path, _cost_sample_at(recent, cost=1.0))
    append_cost_sample(cost_path, _cost_sample_at(recent, cost=2.0))

    # Seed usage.jsonl: an ancient row, a recent row, and a guard row whose
    # ``started_at`` is unparseable (must stay hot).
    _append_usage(mem_fs, usage_path, IterationUsage(
        iteration=1, started_at=ancient.isoformat(),
        task_id="t_ancient", total_cost_usd=9.0, task_cost_usd=9.0,
    ))
    _append_usage(mem_fs, usage_path, IterationUsage(
        iteration=2, started_at=recent.isoformat(),
        task_id="t_recent", total_cost_usd=3.0, task_cost_usd=3.0,
    ))
    _append_usage(mem_fs, usage_path, IterationUsage(
        iteration=3, started_at="not-a-date",
        task_id="t_bad", total_cost_usd=4.0, task_cost_usd=4.0,
    ))

    # Baseline hot-window reads BEFORE the split. The cost-rate cache must be
    # cleared explicitly because ``mem_fs`` never advances ``mtime``.
    _clear_cost_rate_cache()
    burn_before = build_cost_rates(
        JsonlCostSampleStore(cost_path), window_hours=1.0,
    )
    recent_cost_before = cost_for_task(usage_path, "t_recent")
    ancient_cost_before = cost_for_task(usage_path, "t_ancient")

    mem.archive(threshold_days=7)

    cost_archive = str(cost_path) + ".archive.jsonl"
    usage_archive = str(usage_path) + ".archive.jsonl"

    # cost_samples.jsonl: hot keeps the two recent rows, cold holds the ancient.
    hot_cost_ts = [
        json.loads(line)["timestamp"]
        for line in mem_fs[str(cost_path)].splitlines() if line.strip()
    ]
    cold_cost_ts = [
        json.loads(line)["timestamp"]
        for line in mem_fs[cost_archive].splitlines() if line.strip()
    ]
    assert hot_cost_ts == [recent.isoformat(), recent.isoformat()]
    assert cold_cost_ts == [ancient.isoformat()]

    # usage.jsonl: hot keeps the recent + the unparseable row, cold the ancient.
    hot_usage_ids = [
        json.loads(line)["task_id"]
        for line in mem_fs[str(usage_path)].splitlines() if line.strip()
    ]
    cold_usage_ids = [
        json.loads(line)["task_id"]
        for line in mem_fs[usage_archive].splitlines() if line.strip()
    ]
    assert hot_usage_ids == ["t_recent", "t_bad"]  # unparseable stays hot
    assert cold_usage_ids == ["t_ancient"]

    # Burn-rate snapshot is byte-identical over the hot window.
    _clear_cost_rate_cache()
    burn_after = build_cost_rates(
        JsonlCostSampleStore(cost_path), window_hours=1.0,
    )
    assert burn_after == burn_before

    # Per-task aggregate: the in-window task's total is preserved exactly, and
    # the archived task is shed to cold (invisible to the hot reader).
    assert recent_cost_before == 3.0
    assert ancient_cost_before == 9.0
    assert cost_for_task(usage_path, "t_recent") == 3.0
    assert cost_for_task(usage_path, "t_ancient") is None


def test_archive_threshold_above_dashboard_window_keeps_7day_read_stable(
    mem_fs: dict[str, str],
) -> None:
    """The archive threshold must clear the dashboard's 168h (7-day) window.

    ``/api/cost-rates`` / ``/api/budget`` read ``agent.cost_samples`` (the same
    ``cost_samples.jsonl`` this rotates) with ``window_hours`` up to 168h, so
    the widest hot reader is the dashboard, not the 1h loop-internal burn-rate
    reader. A ``threshold_days`` above 7 keeps that 7-day read byte-identical
    across the split; a day-scale threshold at/below 7 sheds an in-window row
    and the dashboard view under-reports (#2571).
    """
    now = datetime.now(UTC)
    recent = now - timedelta(minutes=10)   # inside every window
    mid = now - timedelta(days=6)          # inside the 168h dashboard window
    ancient = now - timedelta(days=10)     # outside 168h, past any safe threshold

    cost_path = FAKE_DIR / "cost_samples.jsonl"
    cost_archive = str(cost_path) + ".archive.jsonl"

    def _seed() -> None:
        # Append ascending by timestamp: the burn-rate reverse-walk stops on the
        # first row older than the cutoff, so append order must stay monotonic.
        append_cost_sample(cost_path, _cost_sample_at(ancient, cost=5.0))
        append_cost_sample(cost_path, _cost_sample_at(mid, cost=7.0))
        append_cost_sample(cost_path, _cost_sample_at(recent, cost=1.0))

    def _hot_cold_ts() -> tuple[list[str], list[str]]:
        hot = [
            json.loads(line)["timestamp"]
            for line in mem_fs[str(cost_path)].splitlines() if line.strip()
        ]
        cold = [
            json.loads(line)["timestamp"]
            for line in mem_fs.get(cost_archive, "").splitlines() if line.strip()
        ]
        return hot, cold

    def _dash_read() -> object:
        # mem_fs never advances mtime, so the cost-rate cache must be cleared
        # before each read to force a rebuild against the trimmed file.
        _clear_cost_rate_cache()
        return build_cost_rates(
            JsonlCostSampleStore(cost_path), window_hours=168.0,
        )

    # --- Safe threshold (> 7 days): the 6-day-old in-window row stays hot, so
    #     the dashboard's 168h read is byte-identical across the split. ---
    mem = _make_memory(mem_fs)
    _seed()
    dash_before = _dash_read()

    mem.archive(threshold_days=8)

    hot_ts, cold_ts = _hot_cold_ts()
    assert hot_ts == [mid.isoformat(), recent.isoformat()]  # in-window row survives
    assert cold_ts == [ancient.isoformat()]
    assert _dash_read() == dash_before

    # --- Unsafe threshold (<= 7 days): the same in-window row is shed to cold,
    #     so the dashboard read changes — the hazard the constraint guards. ---
    mem_fs.pop(str(cost_path), None)
    mem_fs.pop(cost_archive, None)
    mem2 = _make_memory(mem_fs)
    _seed()
    unsafe_before = _dash_read()

    mem2.archive(threshold_days=3)

    unsafe_hot_ts, unsafe_cold_ts = _hot_cold_ts()
    assert unsafe_hot_ts == [recent.isoformat()]  # 6-day-old row wrongly shed
    assert unsafe_cold_ts == [ancient.isoformat(), mid.isoformat()]
    assert _dash_read() != unsafe_before


def test_record_timestamp_normalises_naive_datetime(mem_fs: dict[str, str]) -> None:
    """A naive ISO-8601 timestamp (no tz) is treated as UTC."""
    mem = _make_memory(mem_fs)
    # ``2020-01-01T00:00:00`` has no tz suffix → naive → coerced to UTC.
    naive_old = TaskRecord(
        task_id="naive_old",
        goal="g", plan=[], outcome="success", result_summary="s",
        errors=[], duration_secs=0.0, timestamp="2020-01-01T00:00:00",
    )
    recent = _task_at("recent", days_ago=1)
    mem.record_task(naive_old)
    mem.record_task(recent)

    mem.archive(threshold_days=7)

    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    archive_path = str(FAKE_DIR / "task_history.jsonl.archive.jsonl")
    hot_ids = [json.loads(line)["task_id"] for line in mem_fs[tasks_path].splitlines() if line.strip()]
    cold_ids = [json.loads(line)["task_id"] for line in mem_fs[archive_path].splitlines() if line.strip()]
    assert hot_ids == ["recent"]
    assert cold_ids == ["naive_old"]


def test_build_plan_index_missing_file_returns_empty(mem_fs: dict[str, str]) -> None:
    """``_build_plan_index`` short-circuits when ``_plans_path`` does not exist."""
    mem = _make_memory(mem_fs)
    assert mem._build_plan_index() == {}


def test_build_plan_index_oserror_returns_empty(mem_fs: dict[str, str]) -> None:
    """An OSError during the index build collapses to an empty index."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())

    real_open = _builtins.open
    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")

    def open_raising(file: object, *args: Any, **kwargs: Any) -> Any:
        if str(file) == plans_path:
            raise OSError("locked")
        return real_open(file, *args, **kwargs)

    with patch("builtins.open", side_effect=open_raising):
        assert mem._build_plan_index() == {}


def test_get_plan_oserror_on_seek_returns_none(mem_fs: dict[str, str]) -> None:
    """If reading the indexed line fails, ``get_plan`` returns None."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan(plan_id="ep1"))
    # Warm the cache so ``open`` is called only once during the read.
    mem.get_plan("ep1")

    real_open = _builtins.open
    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")

    def open_raising(file: object, *args: Any, **kwargs: Any) -> Any:
        if str(file) == plans_path:
            raise OSError("torn")
        return real_open(file, *args, **kwargs)

    with patch("builtins.open", side_effect=open_raising):
        assert mem.get_plan("ep1") is None


def test_get_plan_stale_offset_with_no_remaining_record_returns_none(
    mem_fs: dict[str, str],
) -> None:
    """Stale-cookie fallback returns None when the plan is gone from the file."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan(plan_id="ep1"))
    mem.get_plan("ep1")  # warm cache
    # Truncate the file but keep the cached cookie pointing past EOF.
    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    mem_fs[plans_path] = ""
    assert mem.get_plan("ep1") is None
    # Cache was dropped on the stale lookup.
    assert mem._plan_index is None


def test_get_plan_falls_back_when_index_offset_stale(
    mem_fs: dict[str, str],
) -> None:
    """If the file is rewritten without invalidation, get_plan still resolves.

    Simulates a stale offset by warming the index, then rewriting the
    underlying file with the plan placed earlier than the cached cookie
    points at.  The fallback linear-scan path must still find it.
    """
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan(plan_id="ep1"))
    mem.save_evolution_plan(_sample_plan(plan_id="ep2"))
    mem.save_evolution_plan(_sample_plan(plan_id="ep3"))
    mem.get_plan("ep3")  # warm cache
    cached = mem._plan_index
    assert cached is not None and "ep3" in cached

    # Rewrite the file with only ep3 left, but reuse the cached index.
    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    only = json.dumps(asdict(_sample_plan(plan_id="ep3"))) + "\n"
    mem_fs[plans_path] = only

    found = mem.get_plan("ep3")
    assert found is not None and found.plan_id == "ep3"
    # The fallback dropped the stale cache, so the next read rebuilds.
    assert mem._plan_index is None or mem._plan_index.get("ep3") != cached["ep3"]


def test_archive_32_day_threshold_keeps_30_day_and_month_to_date_rows(
    mem_fs: dict[str, str],
) -> None:
    """With a 32-day threshold, a 30-day window and a 31-day sum lose no row."""
    now = datetime.now(UTC)
    in_reading = now - timedelta(days=29.9)
    in_month = now - timedelta(days=31)
    past = now - timedelta(days=33)
    cost_path = FAKE_DIR / "cost_samples.jsonl"
    mem = _make_memory(mem_fs)
    for ts in (past, in_month, in_reading):
        append_cost_sample(cost_path, _cost_sample_at(ts, cost=1.0))

    mem.archive(threshold_days=32)

    hot = [
        json.loads(line)["timestamp"]
        for line in mem_fs[str(cost_path)].splitlines() if line.strip()
    ]
    cold = [
        json.loads(line)["timestamp"]
        for line in mem_fs[str(cost_path) + ".archive.jsonl"].splitlines()
        if line.strip()
    ]
    assert hot == [in_month.isoformat(), in_reading.isoformat()]
    assert cold == [past.isoformat()]
