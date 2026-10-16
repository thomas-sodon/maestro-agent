"""The mechanical orphaned-cure dispose sweep; split out of
``tests/test_memory.py``.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from typing import Any
from unittest.mock import patch

import pytest

from maestro.memory import (
    EvolutionPlan,
    Memory,
    TaskRecord,
    compute_plan_content_hash,
)
from tests.test_memory import (
    _LEVER_LEDGER,
    FAKE_DIR,
    _make_memory,
    _plan,
    _sample_plan,
)

# ---------------------------------------------------------------------------
# Mechanical orphaned-cure dispose sweep — sweep_orphaned_cures
# ---------------------------------------------------------------------------

_CONFLICT_STAMP = "2026-01-01T00:00:00+00:00"


def _implemented_cure(
    mem: Memory,
    plan_id: str = "cure",
    conflict_stamp: str | None = _CONFLICT_STAMP,
) -> None:
    """Seed an ``implemented`` plan with a PR and optional conflict stamp."""
    mem.save_evolution_plan(_plan(
        plan_id,
        "Retire the remediation treadmill at its source",
        "Close the detect-retire gap for orphaned cures",
    ))
    mem.update_plan_status(
        plan_id,
        "implemented",
        pr_url="https://github.com/o/r/pull/1",
        last_conflict_resolution_attempt_at=conflict_stamp,
    )


def _deliver_yes(_plan: EvolutionPlan) -> bool:
    """A disposition deliverer that always permits retirement."""
    return True


def _deliver_no(_plan: EvolutionPlan) -> bool:
    """A disposition deliverer that always refuses retirement (delivery failed)."""
    return False


def _salvage_yes(_plan: EvolutionPlan) -> bool:
    """A salvage predicate that always permits retirement (not SALVAGE_AND_LAND)."""
    return True


def _salvage_no(_plan: EvolutionPlan) -> bool:
    """A salvage predicate that always refuses retirement (SALVAGE_AND_LAND)."""
    return False


def test_sweep_orphaned_cures_retires_aged_conflicted_cure(
    mem_fs: dict[str, str],
) -> None:
    """An implemented cure past the ceiling with conflict evidence is retired."""
    mem = _make_memory(mem_fs)
    _implemented_cure(mem)

    retired = mem.sweep_orphaned_cures(
        {"cure": 10}, 40, 30, deliver_disposition=_deliver_yes,
    )

    assert retired == ["cure"]
    reloaded = mem.get_plan("cure")
    assert reloaded is not None
    assert reloaded.status == "obsolete"
    assert reloaded.obsolete_reason is not None
    assert reloaded.reviewed_at is not None
    # Self-pruning must NOT populate rejection_reason — that channel
    # feeds learn_from_rejection and would mis-classify the prune.
    assert reloaded.rejection_reason is None


def test_sweep_orphaned_cures_captures_one_ledger_row_per_retire(
    mem_fs: dict[str, str],
) -> None:
    """Each retire appends exactly one captured-return row (literal bool)."""
    mem = _make_memory(mem_fs)
    _implemented_cure(mem)

    retired = mem.sweep_orphaned_cures(
        {"cure": 10}, 40, 30, deliver_disposition=_deliver_yes,
    )
    assert retired == ["cure"]

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
    assert row["args"][0] == "cure"
    assert "orphaned-cure sweep" in row["args"][1].lower()


def test_sweep_orphaned_cures_below_ceiling_untouched(
    mem_fs: dict[str, str],
) -> None:
    """A cure below the retire ceiling is not retired and writes no row."""
    mem = _make_memory(mem_fs)
    _implemented_cure(mem)

    retired = mem.sweep_orphaned_cures(
        {"cure": 20}, 40, 30, deliver_disposition=_deliver_yes,
    )

    assert retired == []
    reloaded = mem.get_plan("cure")
    assert reloaded is not None
    assert reloaded.status == "implemented"
    assert _LEVER_LEDGER not in mem_fs


def test_sweep_orphaned_cures_requires_conflict_evidence(
    mem_fs: dict[str, str],
) -> None:
    """An aged cure without a tend-confirmed conflict is never retired."""
    mem = _make_memory(mem_fs)
    _implemented_cure(mem, conflict_stamp=None)

    retired = mem.sweep_orphaned_cures(
        {"cure": 0}, 100, 30, deliver_disposition=_deliver_yes,
    )

    assert retired == []
    reloaded = mem.get_plan("cure")
    assert reloaded is not None
    assert reloaded.status == "implemented"
    assert _LEVER_LEDGER not in mem_fs


def test_sweep_orphaned_cures_ignores_pending_review(
    mem_fs: dict[str, str],
) -> None:
    """A pending_review plan is untouched by the sweep regardless of age."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_plan(
        "pending",
        "Retire the remediation treadmill at its source",
        "Close the detect-retire gap for orphaned cures",
    ))

    retired = mem.sweep_orphaned_cures(
        {"pending": 0}, 100, 30, deliver_disposition=_deliver_yes,
    )

    assert retired == []
    reloaded = mem.get_plan("pending")
    assert reloaded is not None
    assert reloaded.status == "pending_review"
    assert _LEVER_LEDGER not in mem_fs


def test_sweep_orphaned_cures_unknown_plan_has_age_zero(
    mem_fs: dict[str, str],
) -> None:
    """A cure absent from the first-seen map has age 0 and is never retired."""
    mem = _make_memory(mem_fs)
    _implemented_cure(mem)

    retired = mem.sweep_orphaned_cures(
        {}, 100, 30, deliver_disposition=_deliver_yes,
    )

    assert retired == []
    reloaded = mem.get_plan("cure")
    assert reloaded is not None
    assert reloaded.status == "implemented"
    assert _LEVER_LEDGER not in mem_fs


def test_sweep_orphaned_cures_idempotent(
    mem_fs: dict[str, str],
) -> None:
    """A second run over the swept store retires nothing and writes nothing."""
    mem = _make_memory(mem_fs)
    _implemented_cure(mem)

    first = mem.sweep_orphaned_cures(
        {"cure": 10}, 40, 30, deliver_disposition=_deliver_yes,
    )
    assert first == ["cure"]

    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    after_first_plans = mem_fs[plans_path]
    after_first_ledger = mem_fs[_LEVER_LEDGER]

    second = mem.sweep_orphaned_cures(
        {"cure": 10}, 40, 30, deliver_disposition=_deliver_yes,
    )
    assert second == []
    # Idempotent: neither the plans file nor the ledger grows.
    assert mem_fs[plans_path] == after_first_plans
    assert mem_fs[_LEVER_LEDGER] == after_first_ledger


def test_sweep_orphaned_cures_retires_only_after_disposition_delivers(
    mem_fs: dict[str, str],
) -> None:
    """A True deliverer permits retirement: the plan flips obsolete + one row."""
    mem = _make_memory(mem_fs)
    _implemented_cure(mem)
    seen: list[str] = []

    def _deliver(plan: EvolutionPlan) -> bool:
        # The gate hands the deliverer the genuine retire candidate.
        seen.append(plan.plan_id)
        return True

    retired = mem.sweep_orphaned_cures(
        {"cure": 10}, 40, 30, deliver_disposition=_deliver,
    )

    assert retired == ["cure"]
    assert seen == ["cure"]
    reloaded = mem.get_plan("cure")
    assert reloaded is not None
    assert reloaded.status == "obsolete"
    rows = [
        json.loads(line)
        for line in mem_fs[_LEVER_LEDGER].strip().splitlines()
    ]
    assert len(rows) == 1


def test_sweep_orphaned_cures_skips_when_disposition_refused(
    mem_fs: dict[str, str],
) -> None:
    """A False deliverer blocks retirement: the plan stays implemented, no row."""
    mem = _make_memory(mem_fs)
    _implemented_cure(mem)

    retired = mem.sweep_orphaned_cures(
        {"cure": 10}, 40, 30, deliver_disposition=_deliver_no,
    )

    assert retired == []
    reloaded = mem.get_plan("cure")
    assert reloaded is not None
    # Left implemented so tend keeps servicing it — nothing orphaned.
    assert reloaded.status == "implemented"
    # No delivery → no fire → no ledger row.
    assert _LEVER_LEDGER not in mem_fs


def test_sweep_orphaned_cures_skips_when_salvageable(
    mem_fs: dict[str, str],
) -> None:
    """A refusing salvage predicate blocks retirement *before* any disposition is
    delivered: the plan stays implemented, no ledger row, deliver never runs."""
    mem = _make_memory(mem_fs)
    _implemented_cure(mem)
    delivered: list[str] = []

    def _deliver(plan: EvolutionPlan) -> bool:
        # Records any delivery attempt so we can prove the salvage veto is
        # ordered first — a genuine salvage skip must not deliver a disposition.
        delivered.append(plan.plan_id)
        return True

    retired = mem.sweep_orphaned_cures(
        {"cure": 10}, 40, 30,
        deliver_disposition=_deliver,
        salvage_permits_retire=_salvage_no,
    )

    assert retired == []
    # Ordered first: the salvage veto short-circuits before delivery.
    assert delivered == []
    reloaded = mem.get_plan("cure")
    assert reloaded is not None
    assert reloaded.status == "implemented"
    assert _LEVER_LEDGER not in mem_fs


def test_sweep_orphaned_cures_retires_when_salvage_permits(
    mem_fs: dict[str, str],
) -> None:
    """A permitting salvage predicate leaves retirement intact: the plan flips
    obsolete and one ledger row lands, exactly as with the default."""
    mem = _make_memory(mem_fs)
    _implemented_cure(mem)

    retired = mem.sweep_orphaned_cures(
        {"cure": 10}, 40, 30,
        deliver_disposition=_deliver_yes,
        salvage_permits_retire=_salvage_yes,
    )

    assert retired == ["cure"]
    reloaded = mem.get_plan("cure")
    assert reloaded is not None
    assert reloaded.status == "obsolete"
    rows = [
        json.loads(line)
        for line in mem_fs[_LEVER_LEDGER].strip().splitlines()
    ]
    assert len(rows) == 1


def test_sweep_orphaned_cures_default_salvage_permits_retire(
    mem_fs: dict[str, str],
) -> None:
    """Omitting salvage_permits_retire preserves today's behavior (additive): the
    default permissive predicate retires an aged conflict-evidenced cure."""
    mem = _make_memory(mem_fs)
    _implemented_cure(mem)

    retired = mem.sweep_orphaned_cures(
        {"cure": 10}, 40, 30, deliver_disposition=_deliver_yes,
    )

    assert retired == ["cure"]
    reloaded = mem.get_plan("cure")
    assert reloaded is not None
    assert reloaded.status == "obsolete"


def test_save_evolution_plan_gate_blocks_restatement_no_row_written(
    mem_fs: dict[str, str],
) -> None:
    """With the gate on, a restatement is not persisted and row count holds."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_plan(
        "p1",
        "Escalate unlanded remediation to Thomas",
        "Deliver one merge-or-close message when a lineage stalls past threshold",
    ))
    before = len(mem.get_all_plans())
    blocked = mem.save_evolution_plan(
        _plan(
            "p2",
            "Escalate unlanded remediation to Thomas once",
            "Deliver a single merge-or-close message when the lineage stalls past threshold",
        ),
        enforce_restatement_gate=True,
    )
    assert blocked is not None
    assert blocked.plan_id == "p1"
    after = len(mem.get_all_plans())
    assert after == before
    assert mem.get_plan("p2") is None


def test_save_evolution_plan_gate_persists_distinct_plan(
    mem_fs: dict[str, str],
) -> None:
    """With the gate on, a distinct plan is persisted and row count increments."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_plan(
        "p1",
        "Escalate unlanded remediation to Thomas",
        "Deliver one merge-or-close message when a lineage stalls past threshold",
    ))
    before = len(mem.get_all_plans())
    result = mem.save_evolution_plan(
        _plan(
            "p2",
            "Add prometheus latency histogram to dashboard router",
            "Instrument request duration buckets so operators can chart tail latency",
        ),
        enforce_restatement_gate=True,
    )
    assert result is None
    after = len(mem.get_all_plans())
    assert after == before + 1
    assert mem.get_plan("p2") is not None


def test_save_evolution_plan_gate_ignores_non_pending_plans(
    mem_fs: dict[str, str],
) -> None:
    """A restatement of a merged/rejected/obsolete plan is still persisted."""
    mem = _make_memory(mem_fs)
    title = "Escalate unlanded remediation to Thomas"
    rationale = "Deliver one merge-or-close message when a lineage stalls past threshold"
    mem.save_evolution_plan(_plan("p1", title, rationale, status="rejected"))
    result = mem.save_evolution_plan(
        _plan("p2", title, rationale),
        enforce_restatement_gate=True,
    )
    assert result is None
    assert mem.get_plan("p2") is not None


def test_save_evolution_plan_gate_off_by_default(mem_fs: dict[str, str]) -> None:
    """Without the flag, an identical-content plan is persisted (no gate)."""
    mem = _make_memory(mem_fs)
    title = "Escalate unlanded remediation to Thomas"
    rationale = "Deliver one merge-or-close message when a lineage stalls past threshold"
    mem.save_evolution_plan(_plan("p1", title, rationale))
    result = mem.save_evolution_plan(_plan("p2", title, rationale))
    assert result is None
    assert mem.get_plan("p2") is not None


def test_pr_tending_fields_default_to_none() -> None:
    """All EvolutionPlan PR-tending fields default to None."""
    plan = _sample_plan()
    assert plan.last_pr_reminder_at is None
    assert plan.last_conflict_resolution_attempt_at is None
    assert plan.last_quality_fix_attempt_at is None


def test_update_plan_status_sets_last_pr_reminder_at(mem_fs: dict[str, str]) -> None:
    """update_plan_status persists last_pr_reminder_at round-trip."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())

    mem.update_plan_status(
        "ep1", "implemented",
        last_pr_reminder_at="2026-04-22T00:00:00+00:00",
    )

    loaded = mem.get_plan("ep1")
    assert loaded is not None
    assert loaded.last_pr_reminder_at == "2026-04-22T00:00:00+00:00"
    assert loaded.last_conflict_resolution_attempt_at is None


def test_update_plan_status_sets_conflict_attempt(mem_fs: dict[str, str]) -> None:
    """update_plan_status persists last_conflict_resolution_attempt_at round-trip."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())

    mem.update_plan_status(
        "ep1", "implemented",
        last_conflict_resolution_attempt_at="2026-04-22T00:00:00+00:00",
    )

    loaded = mem.get_plan("ep1")
    assert loaded is not None
    assert loaded.last_conflict_resolution_attempt_at == (
        "2026-04-22T00:00:00+00:00"
    )
    assert loaded.last_pr_reminder_at is None


def test_update_plan_status_unset_preserves_fields(mem_fs: dict[str, str]) -> None:
    """Omitting new fields from update_plan_status leaves existing values intact."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    mem.update_plan_status(
        "ep1", "implemented",
        last_pr_reminder_at="2026-04-20T00:00:00+00:00",
        last_conflict_resolution_attempt_at="2026-04-21T00:00:00+00:00",
    )

    mem.update_plan_status("ep1", "implemented", pr_url="https://example/pr/1")

    loaded = mem.get_plan("ep1")
    assert loaded is not None
    assert loaded.last_pr_reminder_at == "2026-04-20T00:00:00+00:00"
    assert loaded.last_conflict_resolution_attempt_at == (
        "2026-04-21T00:00:00+00:00"
    )
    assert loaded.pr_url == "https://example/pr/1"


def test_update_plan_status_rejects_non_string_pr_reminder(mem_fs: dict[str, str]) -> None:
    """Type validation rejects non-string, non-None last_pr_reminder_at."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    with pytest.raises(TypeError, match="last_pr_reminder_at"):
        mem.update_plan_status("ep1", "implemented", last_pr_reminder_at=123)


def test_update_plan_status_rejects_non_string_conflict_attempt(
    mem_fs: dict[str, str],
) -> None:
    """Type validation rejects non-string, non-None last_conflict_resolution_attempt_at."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    with pytest.raises(TypeError, match="last_conflict_resolution_attempt_at"):
        mem.update_plan_status(
            "ep1", "implemented",
            last_conflict_resolution_attempt_at=123,
        )


def test_update_plan_status_sets_quality_fix_attempt(
    mem_fs: dict[str, str],
) -> None:
    """update_plan_status persists last_quality_fix_attempt_at round-trip."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())

    mem.update_plan_status(
        "ep1", "implemented",
        last_quality_fix_attempt_at="2026-04-25T00:00:00+00:00",
    )

    loaded = mem.get_plan("ep1")
    assert loaded is not None
    assert loaded.last_quality_fix_attempt_at == (
        "2026-04-25T00:00:00+00:00"
    )
    # Adjacent fields are preserved.
    assert loaded.last_pr_reminder_at is None
    assert loaded.last_conflict_resolution_attempt_at is None


def test_update_plan_status_rejects_non_string_quality_fix_attempt(
    mem_fs: dict[str, str],
) -> None:
    """Type validation rejects non-string, non-None last_quality_fix_attempt_at."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    with pytest.raises(TypeError, match="last_quality_fix_attempt_at"):
        mem.update_plan_status(
            "ep1", "implemented",
            last_quality_fix_attempt_at=123,
        )


def test_update_plan_status_rejects_non_int_failure_count(
    mem_fs: dict[str, str],
) -> None:
    """Type validation rejects non-int failure_count."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    with pytest.raises(TypeError, match="failure_count"):
        mem.update_plan_status("ep1", "failed", failure_count="1")


def test_update_plan_status_rejects_non_string_last_failure_at(
    mem_fs: dict[str, str],
) -> None:
    """Type validation rejects non-string, non-None last_failure_at."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    with pytest.raises(TypeError, match="last_failure_at"):
        mem.update_plan_status("ep1", "failed", last_failure_at=123)


def test_update_plan_status_sets_ready_for_review_pending_sha(
    mem_fs: dict[str, str],
) -> None:
    """update_plan_status persists and (with None) clears the pending head SHA."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())

    mem.update_plan_status(
        "ep1", "implemented", ready_for_review_pending_sha="deadbeef",
    )
    loaded = mem.get_plan("ep1")
    assert loaded is not None
    assert loaded.ready_for_review_pending_sha == "deadbeef"

    # ``None`` is a real value (distinct from the unset sentinel) — it clears.
    mem.update_plan_status(
        "ep1", "implemented", ready_for_review_pending_sha=None,
    )
    cleared = mem.get_plan("ep1")
    assert cleared is not None
    assert cleared.ready_for_review_pending_sha is None


def test_update_plan_status_rejects_non_string_ready_for_review_pending_sha(
    mem_fs: dict[str, str],
) -> None:
    """Type validation rejects a non-string, non-None pending head SHA."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    with pytest.raises(TypeError, match="ready_for_review_pending_sha"):
        mem.update_plan_status(
            "ep1", "implemented", ready_for_review_pending_sha=123,
        )


def test_update_plan_status_rejects_non_string_last_failure_reason(
    mem_fs: dict[str, str],
) -> None:
    """Type validation rejects non-string, non-None last_failure_reason."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    with pytest.raises(TypeError, match="last_failure_reason"):
        mem.update_plan_status("ep1", "failed", last_failure_reason=["not", "a", "str"])


def test_update_plan_status_sets_failure_fields(mem_fs: dict[str, str]) -> None:
    """Happy-path: failure_count / last_failure_at / last_failure_reason
    persist through save+load."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    assert mem.update_plan_status(
        "ep1", "failed",
        failure_count=2,
        last_failure_at="2026-04-23T10:00:00+00:00",
        last_failure_reason="boom",
    )
    reloaded = mem.get_plan("ep1")
    assert reloaded is not None
    assert reloaded.status == "failed"
    assert reloaded.failure_count == 2
    assert reloaded.last_failure_at == "2026-04-23T10:00:00+00:00"
    assert reloaded.last_failure_reason == "boom"


def test_update_plan_status_sets_pr_reminder_count(
    mem_fs: dict[str, str],
) -> None:
    """update_plan_status persists pr_reminder_count round-trip.

    Exercised directly here because the tend-flow reminder path now stamps this
    field through ``update_plan_tend_fields`` (proposal 091), not
    ``update_plan_status`` — the method's own contract still supports it."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    mem.update_plan_status("ep1", "implemented", pr_reminder_count=4)
    loaded = mem.get_plan("ep1")
    assert loaded is not None
    assert loaded.pr_reminder_count == 4


def test_update_plan_status_expect_status_match_succeeds(
    mem_fs: dict[str, str],
) -> None:
    """A matching ``expect_status`` mutates the row and returns True."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    assert mem.update_plan_status(
        "ep1", "approved",
        reviewed_at="2026-04-25T00:00:00+00:00",
        expect_status="pending_review",
    ) is True
    plan = mem.get_plan("ep1")
    assert plan is not None
    assert plan.status == "approved"
    assert plan.reviewed_at == "2026-04-25T00:00:00+00:00"


def test_update_plan_status_expect_status_mismatch_returns_false(
    mem_fs: dict[str, str],
) -> None:
    """Lock-checked status mismatch returns False without mutating the row.

    The cross-surface window: the dashboard and Telegram both read-then-write,
    so without the under-lock recheck the loser silently clobbers the human
    decision the winner already recorded.
    """
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    # First surface wins: pending_review → approved.
    assert mem.update_plan_status(
        "ep1", "approved",
        reviewed_at="2026-04-25T00:00:00+00:00",
        expect_status="pending_review",
    ) is True
    # Second surface tries to flip to rejected, still expecting pending.
    assert mem.update_plan_status(
        "ep1", "rejected",
        rejection_reason="too late",
        reviewed_at="2026-04-25T00:00:01+00:00",
        expect_status="pending_review",
    ) is False
    # The winner's write is preserved verbatim — no field leaked through.
    plan = mem.get_plan("ep1")
    assert plan is not None
    assert plan.status == "approved"
    assert plan.rejection_reason is None
    assert plan.reviewed_at == "2026-04-25T00:00:00+00:00"


def test_update_plan_status_expect_status_default_is_unconditional(
    mem_fs: dict[str, str],
) -> None:
    """The ``None`` default preserves the legacy unconditional flip.

    The implementation / merge / tend callers depend on it: they re-pass
    ``plan.status`` for side-channel field updates and must not gain a
    precondition.
    """
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    assert mem.update_plan_status("ep1", "approved") is True
    # Current status is now ``approved`` and no expectation is declared,
    # so the next flip still lands.
    assert mem.update_plan_status("ep1", "implemented") is True
    plan = mem.get_plan("ep1")
    assert plan is not None
    assert plan.status == "implemented"


def test_update_plan_status_expect_status_unknown_id_returns_false(
    mem_fs: dict[str, str],
) -> None:
    """Missing plan returns False even with expect_status set — no exception."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    assert mem.update_plan_status(
        "no-such-plan", "approved", expect_status="pending_review",
    ) is False
    surviving = mem.get_plan("ep1")
    assert surviving is not None
    assert surviving.status == "pending_review"


def test_update_plan_tend_fields_persists_without_touching_status(
    mem_fs: dict[str, str],
) -> None:
    """update_plan_tend_fields writes tend aux fields on an *obsolete* plan
    while leaving status/obsolete_reason untouched — the whole reason it
    exists (``update_plan_status`` refuses an ``obsolete`` write)."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    mem.update_plan_status("ep1", "implemented")
    assert mem.mark_plan_obsolete(
        "ep1", "retired", allow_implemented=True,
    ) is True

    # First write touches only last_pr_reminder_at — the other four ifs skip.
    assert mem.update_plan_tend_fields(
        "ep1", last_pr_reminder_at="2026-05-01T00:00:00+00:00",
    ) is True
    after_first = mem.get_plan("ep1")
    assert after_first is not None
    assert after_first.status == "obsolete"
    assert after_first.obsolete_reason == "retired"
    assert after_first.last_pr_reminder_at == "2026-05-01T00:00:00+00:00"
    assert after_first.pr_reminder_count == 0
    assert after_first.last_conflict_resolution_attempt_at is None
    assert after_first.last_conflict_resolution_base_sha is None
    assert after_first.last_quality_fix_attempt_at is None
    assert after_first.ready_for_review_pending_sha is None
    # Proposal 137's six signature fields also skip on the unset sentinel.
    assert after_first.last_conflict_resolution_head_sha is None
    assert after_first.last_conflict_resolution_ci_conclusion is None
    assert after_first.last_quality_fix_head_sha is None
    assert after_first.last_quality_fix_base_sha is None
    assert after_first.last_quality_fix_ci_conclusion is None
    # Proposal 137's two per-signature attempt counters likewise skip, and a
    # legacy row reads back as ``0``.
    assert after_first.conflict_resolution_signature_attempts == 0
    assert after_first.quality_fix_signature_attempts == 0

    # Second write touches the others — last_pr_reminder_at now skips,
    # so its prior value is preserved (the unset-sentinel path).
    assert mem.update_plan_tend_fields(
        "ep1",
        pr_reminder_count=3,
        last_conflict_resolution_attempt_at="2026-05-02T00:00:00+00:00",
        last_conflict_resolution_base_sha="beef5678",
        last_conflict_resolution_head_sha="head1111",
        last_conflict_resolution_ci_conclusion="failure",
        last_quality_fix_attempt_at="2026-05-03T00:00:00+00:00",
        last_quality_fix_head_sha="head2222",
        last_quality_fix_base_sha="base3333",
        last_quality_fix_ci_conclusion="success",
        conflict_resolution_signature_attempts=2,
        quality_fix_signature_attempts=5,
        ready_for_review_pending_sha="cafe1234",
    ) is True
    after_second = mem.get_plan("ep1")
    assert after_second is not None
    # Status / obsolete_reason still untouched — no forbidden status write.
    assert after_second.status == "obsolete"
    assert after_second.obsolete_reason == "retired"
    assert after_second.last_pr_reminder_at == "2026-05-01T00:00:00+00:00"
    assert after_second.pr_reminder_count == 3
    assert after_second.last_conflict_resolution_attempt_at == (
        "2026-05-02T00:00:00+00:00"
    )
    assert after_second.last_conflict_resolution_base_sha == "beef5678"
    assert after_second.last_quality_fix_attempt_at == (
        "2026-05-03T00:00:00+00:00"
    )
    assert after_second.ready_for_review_pending_sha == "cafe1234"
    assert after_second.last_conflict_resolution_head_sha == "head1111"
    assert after_second.last_conflict_resolution_ci_conclusion == "failure"
    assert after_second.last_quality_fix_head_sha == "head2222"
    assert after_second.last_quality_fix_base_sha == "base3333"
    assert after_second.last_quality_fix_ci_conclusion == "success"
    assert after_second.conflict_resolution_signature_attempts == 2
    assert after_second.quality_fix_signature_attempts == 5


def test_update_plan_tend_fields_missing_plan_returns_false(
    mem_fs: dict[str, str],
) -> None:
    """A tend-field write against an unknown plan_id returns False."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    assert mem.update_plan_tend_fields(
        "nonexistent", last_pr_reminder_at="2026-05-01T00:00:00+00:00",
    ) is False


def test_update_plan_tend_fields_type_validation(mem_fs: dict[str, str]) -> None:
    """Each field enforces the same type contract as update_plan_status."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    with pytest.raises(TypeError, match="last_pr_reminder_at"):
        mem.update_plan_tend_fields("ep1", last_pr_reminder_at=123)
    with pytest.raises(TypeError, match="pr_reminder_count"):
        mem.update_plan_tend_fields("ep1", pr_reminder_count="1")
    with pytest.raises(TypeError, match="last_conflict_resolution_attempt_at"):
        mem.update_plan_tend_fields(
            "ep1", last_conflict_resolution_attempt_at=123,
        )
    with pytest.raises(TypeError, match="last_conflict_resolution_base_sha"):
        mem.update_plan_tend_fields(
            "ep1", last_conflict_resolution_base_sha=123,
        )
    with pytest.raises(TypeError, match="last_quality_fix_attempt_at"):
        mem.update_plan_tend_fields("ep1", last_quality_fix_attempt_at=123)
    with pytest.raises(TypeError, match="ready_for_review_pending_sha"):
        mem.update_plan_tend_fields("ep1", ready_for_review_pending_sha=123)
    # Proposal 137's six signature fields carry the same str-or-None contract.
    with pytest.raises(TypeError, match="last_conflict_resolution_head_sha"):
        mem.update_plan_tend_fields(
            "ep1", last_conflict_resolution_head_sha=123,
        )
    with pytest.raises(
        TypeError, match="last_conflict_resolution_ci_conclusion",
    ):
        mem.update_plan_tend_fields(
            "ep1", last_conflict_resolution_ci_conclusion=123,
        )
    with pytest.raises(TypeError, match="last_quality_fix_head_sha"):
        mem.update_plan_tend_fields("ep1", last_quality_fix_head_sha=123)
    with pytest.raises(TypeError, match="last_quality_fix_base_sha"):
        mem.update_plan_tend_fields("ep1", last_quality_fix_base_sha=123)
    with pytest.raises(TypeError, match="last_quality_fix_ci_conclusion"):
        mem.update_plan_tend_fields("ep1", last_quality_fix_ci_conclusion=123)
    # Proposal 137's attempt counters take the int contract pr_reminder_count
    # already carries.
    with pytest.raises(TypeError, match="conflict_resolution_signature_attempts"):
        mem.update_plan_tend_fields(
            "ep1", conflict_resolution_signature_attempts="2",
        )
    with pytest.raises(TypeError, match="quality_fix_signature_attempts"):
        mem.update_plan_tend_fields("ep1", quality_fix_signature_attempts=None)


def test_update_plan_inherited_hold_persists_without_touching_status(
    mem_fs: dict[str, str],
) -> None:
    """update_plan_inherited_hold (proposal 124) writes the hold fields while
    leaving status and failure_count untouched — the whole reason it exists is to
    hold a plan ``approved`` on an ``INHERITED`` terminal instead of demoting."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    mem.update_plan_status("ep1", "approved")  # the status the hold preserves

    assert mem.update_plan_inherited_hold(
        "ep1", inherited_hold_main_sha="beefcafe", inherited_hold_count=1,
    ) is True
    after_first = mem.get_plan("ep1")
    assert after_first is not None
    assert after_first.status == "approved"
    assert after_first.failure_count == 0
    assert after_first.inherited_hold_main_sha == "beefcafe"
    assert after_first.inherited_hold_count == 1

    # A second write touches only the count — the SHA skips (unset sentinel) and
    # its prior value is preserved.
    assert mem.update_plan_inherited_hold(
        "ep1", inherited_hold_count=2,
    ) is True
    after_second = mem.get_plan("ep1")
    assert after_second is not None
    assert after_second.status == "approved"
    assert after_second.inherited_hold_main_sha == "beefcafe"
    assert after_second.inherited_hold_count == 2

    # An unmeasurable base clears the SHA back to None while still holding.
    assert mem.update_plan_inherited_hold(
        "ep1", inherited_hold_main_sha=None,
    ) is True
    after_third = mem.get_plan("ep1")
    assert after_third is not None
    assert after_third.inherited_hold_main_sha is None
    assert after_third.inherited_hold_count == 2


def test_update_plan_inherited_hold_missing_plan_returns_false(
    mem_fs: dict[str, str],
) -> None:
    """A hold write against an unknown plan_id returns False."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    assert mem.update_plan_inherited_hold(
        "nonexistent", inherited_hold_count=1,
    ) is False


def test_update_plan_inherited_hold_type_validation(mem_fs: dict[str, str]) -> None:
    """The hold writer enforces string-or-None SHA and int count."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    with pytest.raises(TypeError, match="inherited_hold_main_sha"):
        mem.update_plan_inherited_hold("ep1", inherited_hold_main_sha=123)
    with pytest.raises(TypeError, match="inherited_hold_count"):
        mem.update_plan_inherited_hold("ep1", inherited_hold_count="1")


def test_update_plan_environment_hold_persists_without_touching_status(
    mem_fs: dict[str, str],
) -> None:
    """update_plan_environment_hold (proposal 133) stamps the fingerprint while
    leaving status and failure_count untouched — the hold exists precisely because
    *no* implementation attempt was spent, so neither may move."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    mem.update_plan_status("ep1", "approved")

    assert mem.update_plan_environment_hold(
        "ep1", environment_hold_fingerprint="cafe1234",
    ) is True
    held = mem.get_plan("ep1")
    assert held is not None
    assert held.status == "approved"
    assert held.failure_count == 0
    assert held.environment_hold_fingerprint == "cafe1234"

    # The fingerprint moves while the fact stands -> a re-stamp, still approved.
    assert mem.update_plan_environment_hold(
        "ep1", environment_hold_fingerprint="beef5678",
    ) is True
    restamped = mem.get_plan("ep1")
    assert restamped is not None
    assert restamped.status == "approved"
    assert restamped.environment_hold_fingerprint == "beef5678"

    # Released: ``None`` clears the stamp so it cannot outlive its hold.
    assert mem.update_plan_environment_hold(
        "ep1", environment_hold_fingerprint=None,
    ) is True
    released = mem.get_plan("ep1")
    assert released is not None
    assert released.status == "approved"
    assert released.environment_hold_fingerprint is None


def test_update_plan_environment_hold_unset_leaves_the_field(
    mem_fs: dict[str, str],
) -> None:
    """Calling with no field skips the write, preserving the prior stamp."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    mem.update_plan_environment_hold("ep1", environment_hold_fingerprint="abc")

    assert mem.update_plan_environment_hold("ep1") is True
    after = mem.get_plan("ep1")
    assert after is not None
    assert after.environment_hold_fingerprint == "abc"


def test_update_plan_environment_hold_missing_plan_returns_false(
    mem_fs: dict[str, str],
) -> None:
    """An environment-hold write against an unknown plan_id returns False."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    assert mem.update_plan_environment_hold(
        "nonexistent", environment_hold_fingerprint="abc",
    ) is False


def test_update_plan_environment_hold_type_validation(
    mem_fs: dict[str, str],
) -> None:
    """The environment-hold writer enforces a string-or-None fingerprint."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    with pytest.raises(TypeError, match="environment_hold_fingerprint"):
        mem.update_plan_environment_hold("ep1", environment_hold_fingerprint=123)


def test_update_plans_environment_hold_stamps_the_set_in_one_write(
    mem_fs: dict[str, str],
) -> None:
    """The batched writer stamps every named plan, ignores unknown ids, leaves
    status/failure_count alone, and does a single save for the whole set."""
    mem = _make_memory(mem_fs)
    for plan_id in ("ep1", "ep2", "ep3"):
        plan = _sample_plan()
        plan.plan_id = plan_id
        mem.save_evolution_plan(plan)
    mem.update_plan_status("ep1", "approved")

    with patch.object(
        mem, "_save_all_plans", wraps=mem._save_all_plans,
    ) as save:
        updated = mem.update_plans_environment_hold(
            ["ep1", "ep2", "missing"], environment_hold_fingerprint="fp1",
        )

    assert updated == 2
    assert save.call_count == 1
    first = mem.get_plan("ep1")
    assert first is not None
    assert first.environment_hold_fingerprint == "fp1"
    assert first.status == "approved"
    assert first.failure_count == 0
    second = mem.get_plan("ep2")
    assert second is not None
    assert second.environment_hold_fingerprint == "fp1"
    untouched = mem.get_plan("ep3")
    assert untouched is not None
    assert untouched.environment_hold_fingerprint is None


def test_update_plans_environment_hold_clears_the_set(
    mem_fs: dict[str, str],
) -> None:
    """``None`` clears the stamp across the set, which is how a released hold
    sheds fingerprints that would otherwise outlive it."""
    mem = _make_memory(mem_fs)
    for plan_id in ("ep1", "ep2"):
        plan = _sample_plan()
        plan.plan_id = plan_id
        mem.save_evolution_plan(plan)
    mem.update_plans_environment_hold(
        ["ep1", "ep2"], environment_hold_fingerprint="fp1",
    )

    assert mem.update_plans_environment_hold(
        ["ep1", "ep2"], environment_hold_fingerprint=None,
    ) == 2
    for plan_id in ("ep1", "ep2"):
        cleared = mem.get_plan(plan_id)
        assert cleared is not None
        assert cleared.environment_hold_fingerprint is None


def test_update_plans_environment_hold_writes_nothing_without_matches(
    mem_fs: dict[str, str],
) -> None:
    """An empty id set, or one matching no plan, takes no write at all."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())

    with patch.object(mem, "_save_all_plans") as save:
        assert mem.update_plans_environment_hold(
            [], environment_hold_fingerprint="fp1",
        ) == 0
        assert mem.update_plans_environment_hold(
            ["missing"], environment_hold_fingerprint="fp1",
        ) == 0

    save.assert_not_called()


def test_update_plans_environment_hold_type_validation(
    mem_fs: dict[str, str],
) -> None:
    """The batched writer enforces the same string-or-None fingerprint."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    with pytest.raises(TypeError, match="environment_hold_fingerprint"):
        mem.update_plans_environment_hold(
            ["ep1"], environment_hold_fingerprint=123,  # type: ignore[arg-type]
        )


def test_save_plan_round_trips_workspace_citation(mem_fs: dict[str, str]) -> None:
    """An EvolutionPlan carrying conscious-access citation provenance survives
    save + load with values intact (proposal 130)."""
    mem = _make_memory(mem_fs)
    plan = _plan("p1", "title", "rationale")
    plan.workspace_moment_id = "moment-xyz"
    plan.workspace_item_ids = ["item-a", "item-b"]
    mem.save_evolution_plan(plan)

    got = mem.get_plan("p1")
    assert got is not None
    assert got.workspace_moment_id == "moment-xyz"
    assert got.workspace_item_ids == ["item-a", "item-b"]


def test_plan_workspace_item_ids_empty_list_distinct_from_none(
    mem_fs: dict[str, str],
) -> None:
    """A plan citing the moment but no item ([]) round-trips as [], not None —
    _parse_plan_line must preserve the None-vs-[] distinction (proposal 130)."""
    mem = _make_memory(mem_fs)
    plan = _plan("p1", "title", "rationale")
    plan.workspace_moment_id = "moment-xyz"
    plan.workspace_item_ids = []
    mem.save_evolution_plan(plan)

    got = mem.get_plan("p1")
    assert got is not None
    assert got.workspace_item_ids == []


def test_legacy_plan_row_deserializes_workspace_citation_as_none(
    mem_fs: dict[str, str],
) -> None:
    """A legacy plan row lacking both citation fields loads with None defaults
    (proposal 130)."""
    mem = _make_memory(mem_fs)
    legacy = {
        "plan_id": "p1", "title": "t", "stage": "awakening",
        "category": "capability", "rationale": "r",
        "proposed_changes": [], "files_affected": [],
        "expected_impact": "", "status": "pending_review",
        "created_at": "2025-01-01T00:00:00Z",
    }
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = json.dumps(legacy) + "\n"

    got = mem.get_plan("p1")
    assert got is not None
    assert got.workspace_moment_id is None
    assert got.workspace_item_ids is None


def test_plan_workspace_item_ids_malformed_coerces(mem_fs: dict[str, str]) -> None:
    """_parse_plan_line coerces workspace_item_ids: non-str members are dropped
    from a list, and a non-list value (even a str) collapses to None — it is not
    fed through the proposed_changes-style line splitter (proposal 130)."""
    mem = _make_memory(mem_fs)
    base = {
        "title": "t", "stage": "awakening", "category": "capability",
        "rationale": "r", "proposed_changes": [], "files_affected": [],
        "expected_impact": "", "status": "pending_review",
        "created_at": "2025-01-01T00:00:00Z",
    }
    rows = [
        {**base, "plan_id": "p-list",
         "workspace_item_ids": ["ok", 3, None, "also-ok"]},
        {**base, "plan_id": "p-nonlist", "workspace_item_ids": "not-a-list"},
    ]
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = "".join(
        json.dumps(r) + "\n" for r in rows
    )

    listed = mem.get_plan("p-list")
    assert listed is not None
    assert listed.workspace_item_ids == ["ok", "also-ok"]
    nonlist = mem.get_plan("p-nonlist")
    assert nonlist is not None
    assert nonlist.workspace_item_ids is None


def test_plan_content_hash_stable_across_workspace_citation() -> None:
    """compute_plan_content_hash is byte-identical whether or not the citation
    fields are populated — the allowlist keeps an approved plan's approval hash
    unchanged when these provenance fields appear (proposal 130)."""
    bare = _plan("p1", "title", "rationale")
    cited = _plan("p1", "title", "rationale")
    cited.workspace_moment_id = "moment-xyz"
    cited.workspace_item_ids = ["item-a", "item-b"]

    assert compute_plan_content_hash(bare) == compute_plan_content_hash(cited)


def test_inherited_hold_count_float_coercion(mem_fs: dict[str, str]) -> None:
    """A float-round-tripped inherited_hold_count loads back as int, so it never
    compares wrong against max_implementation_attempts."""
    mem = _make_memory(mem_fs)
    row = {
        "plan_id": "ep_float",
        "title": "legacy",
        "stage": "awakening",
        "category": "capability",
        "rationale": "r",
        "proposed_changes": [],
        "files_affected": [],
        "expected_impact": "",
        "priority": "capability",
        "status": "approved",
        "created_at": "2024-01-01T00:00:00Z",
        "inherited_hold_count": 2.0,
    }
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = json.dumps(row) + "\n"

    loaded = mem.get_plan("ep_float")
    assert loaded is not None
    assert loaded.inherited_hold_count == 2
    assert isinstance(loaded.inherited_hold_count, int)


def test_blocked_plan_excluded_from_pending_and_live(
    mem_fs: dict[str, str],
) -> None:
    """A ``blocked`` plan (proposal 131) is inert like ``sliced``: it is omitted
    from both get_pending_plans() and get_live_plan_records() so it is dormant on
    every eligibility/live surface until re-admitted."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_plan("p-pending", "t", "r", status="pending_review"))
    mem.save_evolution_plan(_plan("p-approved", "t", "r", status="approved"))
    mem.save_evolution_plan(_plan("p-blocked", "t", "r", status="blocked"))

    pending_ids = {p.plan_id for p in mem.get_pending_plans()}
    live_ids = {p.plan_id for p in mem.get_live_plan_records()}
    # ``_implement_approved_plans`` reads ``get_all_plans()`` and picks up only
    # ``approved`` / eligible-``failed`` rows; a ``blocked`` plan is neither, so it
    # is inert on the implementation-eligibility surface too.
    eligible_ids = {
        p.plan_id for p in mem.get_all_plans()
        if p.status == "approved" or p.status == "failed"
    }

    assert "p-blocked" not in pending_ids
    assert "p-blocked" not in live_ids
    assert "p-blocked" not in eligible_ids
    assert pending_ids == {"p-pending"}
    assert live_ids == {"p-pending", "p-approved"}


def test_update_plan_status_round_trips_blocked(mem_fs: dict[str, str]) -> None:
    """update_plan_status persists the ``blocked`` status round-trip (proposal
    131); it is not the ``obsolete`` path, so no dedicated writer is needed."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())

    updated = mem.update_plan_status("ep1", "blocked")

    assert updated is True
    loaded = mem.get_plan("ep1")
    assert loaded is not None
    assert loaded.status == "blocked"


def test_update_plan_status_round_trips_blocked_question_id(
    mem_fs: dict[str, str],
) -> None:
    """update_plan_status persists ``blocked_question_id`` atomically with the
    ``blocked`` flip (proposal 131) — the reloaded plan carries both the status
    and the recorded ``ask_thomas`` message id, and a bad type is rejected."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())

    updated = mem.update_plan_status(
        "ep1", "blocked",
        blocked_question_id="deadbeef1234",
        last_failure_reason="recurring environment red",
    )

    assert updated is True
    loaded = mem.get_plan("ep1")
    assert loaded is not None
    assert loaded.status == "blocked"
    assert loaded.blocked_question_id == "deadbeef1234"
    assert loaded.last_failure_reason == "recurring environment red"

    with pytest.raises(TypeError):
        mem.update_plan_status("ep1", "blocked", blocked_question_id=123)


def test_resume_count_float_coercion(mem_fs: dict[str, str]) -> None:
    """A float-round-tripped resume_count loads back as int, so it never compares
    wrong against the future max_resume_attempts budget (proposal 131)."""
    mem = _make_memory(mem_fs)
    row = {
        "plan_id": "ep_resume_float",
        "title": "legacy",
        "stage": "awakening",
        "category": "capability",
        "rationale": "r",
        "proposed_changes": [],
        "files_affected": [],
        "expected_impact": "",
        "priority": "capability",
        "status": "approved",
        "created_at": "2024-01-01T00:00:00Z",
        "resume_count": 3.0,
    }
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = json.dumps(row) + "\n"

    loaded = mem.get_plan("ep_resume_float")
    assert loaded is not None
    assert loaded.resume_count == 3
    assert isinstance(loaded.resume_count, int)


def test_legacy_plan_row_defaults_resume_and_blocked_question(
    mem_fs: dict[str, str],
) -> None:
    """A legacy plan row lacking every proposal-131 field loads with the dormant
    defaults (``resume_count=0`` / ``resume_failing_identities=[]`` /
    ``resume_branch_head=None`` / ``blocked_question_id=None``)."""
    mem = _make_memory(mem_fs)
    legacy = {
        "plan_id": "p1", "title": "t", "stage": "awakening",
        "category": "capability", "rationale": "r",
        "proposed_changes": [], "files_affected": [],
        "expected_impact": "", "status": "pending_review",
        "created_at": "2025-01-01T00:00:00Z",
    }
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = json.dumps(legacy) + "\n"

    got = mem.get_plan("p1")
    assert got is not None
    assert got.resume_count == 0
    assert got.resume_failing_identities == []
    assert got.resume_branch_head is None
    assert got.blocked_question_id is None


def test_update_plan_resume_round_trips_carry_context(
    mem_fs: dict[str, str],
) -> None:
    """update_plan_resume (proposal 131) round-trips the RESUME carry-context —
    resume_count, the failing-identity baseline, the preserved branch head, and
    the failure reason — without touching status or failure_count."""
    mem = _make_memory(mem_fs)
    plan = _sample_plan()
    plan.status = "failed"
    plan.failure_count = 1
    mem.save_evolution_plan(plan)

    assert mem.update_plan_resume(
        "ep1",
        resume_count=2,
        resume_failing_identities=["tests:a", "tests:b"],
        resume_branch_head="deadbeef" + "0" * 32,
        last_failure_reason="progressing stall",
    ) is True

    loaded = mem.get_plan("ep1")
    assert loaded is not None
    assert loaded.resume_count == 2
    assert loaded.resume_failing_identities == ["tests:a", "tests:b"]
    assert loaded.resume_branch_head == "deadbeef" + "0" * 32
    assert loaded.last_failure_reason == "progressing stall"
    # Status and failure_count are deliberately left frozen.
    assert loaded.status == "failed"
    assert loaded.failure_count == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"resume_count": "2"},
        {"resume_failing_identities": ["ok", 3]},
        {"resume_failing_identities": "not-a-list"},
        {"resume_branch_head": 123},
        {"last_failure_reason": 123},
    ],
)
def test_update_plan_resume_type_validation(
    mem_fs: dict[str, str], kwargs: dict[str, object],
) -> None:
    """update_plan_resume type-validates every field like its inherited-hold
    sibling — a wrong-typed value raises rather than persisting junk."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())

    with pytest.raises(TypeError):
        mem.update_plan_resume("ep1", **kwargs)  # type: ignore[arg-type]


def test_update_plan_resume_missing_plan_returns_false(
    mem_fs: dict[str, str],
) -> None:
    """update_plan_resume returns False when the plan id is unknown."""
    mem = _make_memory(mem_fs)
    assert mem.update_plan_resume("ghost", resume_count=1) is False


def test_plan_content_hash_stable_across_resume_fields() -> None:
    """compute_plan_content_hash is byte-identical whether or not the proposal-131
    resume fields are populated — they are bookkeeping outside the content
    allowlist, so an approved plan's approval hash never shifts (proposal 131)."""
    bare = _plan("p1", "title", "rationale")
    resumed = _plan("p1", "title", "rationale")
    resumed.resume_count = 2
    resumed.resume_failing_identities = ["tests:a", "tests:b"]
    resumed.resume_branch_head = "deadbeef" + "0" * 32

    assert compute_plan_content_hash(bare) == compute_plan_content_hash(resumed)


def test_resume_failing_identities_normalised_on_read(
    mem_fs: dict[str, str],
) -> None:
    """resume_failing_identities is normalised on read exactly like its sibling
    list fields — a present list keeps only its string members, and a value that
    is neither a list nor a string collapses to []."""
    mem = _make_memory(mem_fs)
    base = {
        "title": "t", "stage": "awakening", "category": "capability",
        "rationale": "r", "proposed_changes": [], "files_affected": [],
        "expected_impact": "", "status": "failed",
        "created_at": "2025-01-01T00:00:00Z",
    }
    rows = [
        {**base, "plan_id": "p-list",
         "resume_failing_identities": ["ok", 3, None, "also-ok"]},
        {**base, "plan_id": "p-nonlist",
         "resume_failing_identities": 42},
    ]
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = "".join(
        json.dumps(r) + "\n" for r in rows
    )

    listed = mem.get_plan("p-list")
    assert listed is not None
    assert listed.resume_failing_identities == ["ok", "also-ok"]
    nonlist = mem.get_plan("p-nonlist")
    assert nonlist is not None
    assert nonlist.resume_failing_identities == []


def test_update_plan_proposed_changes_replaces_list(
    mem_fs: dict[str, str],
) -> None:
    """update_plan_proposed_changes round-trips a new list of strings."""
    mem = _make_memory(mem_fs)
    plan = _sample_plan()
    plan.proposed_changes = ["original step 1", "original step 2"]
    mem.save_evolution_plan(plan)

    rewritten = [
        "step 1; tests added in tests/test_x.py verify it",
        "step 2; verify integration via tests/test_y.py",
    ]
    assert mem.update_plan_proposed_changes("ep1", rewritten) is True

    loaded = mem.get_plan("ep1")
    assert loaded is not None
    assert loaded.proposed_changes == rewritten


def test_update_plan_proposed_changes_invalidates_index(
    mem_fs: dict[str, str],
) -> None:
    """The lazy line-offset index is invalidated after a write so
    subsequent ``get_plan`` calls see the new content, not stale rows."""
    mem = _make_memory(mem_fs)
    plan = _sample_plan()
    plan.proposed_changes = ["old"]
    mem.save_evolution_plan(plan)

    # Prime the offset index (the lazy index is built on first use).
    _ = mem.get_plan("ep1")

    # Mutate via the new method.
    mem.update_plan_proposed_changes("ep1", ["new"])

    # A subsequent read must reflect the new content even though the
    # index was previously primed.
    loaded = mem.get_plan("ep1")
    assert loaded is not None
    assert loaded.proposed_changes == ["new"]


def test_update_plan_proposed_changes_returns_false_for_unknown_id(
    mem_fs: dict[str, str],
) -> None:
    """Missing plan id is a no-op that returns False."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    assert mem.update_plan_proposed_changes("does-not-exist", ["x"]) is False


def test_update_plan_proposed_changes_rejects_non_list_str(
    mem_fs: dict[str, str],
) -> None:
    """Type validation rejects non-list and lists containing non-strings."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    with pytest.raises(TypeError, match="proposed_changes"):
        mem.update_plan_proposed_changes("ep1", "not a list")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="proposed_changes"):
        mem.update_plan_proposed_changes("ep1", ["ok", 42])  # type: ignore[list-item]


def test_refine_plan_replaces_only_specified_fields(
    mem_fs: dict[str, str],
) -> None:
    """refine_plan only updates fields explicitly passed; others retained."""
    mem = _make_memory(mem_fs)
    plan = _sample_plan()
    plan.rationale = "original rationale"
    plan.proposed_changes = ["original step"]
    plan.acceptance_criteria = ["original criterion"]
    plan.files_affected = ["maestro/x.py"]
    mem.save_evolution_plan(plan)

    assert mem.refine_plan(
        "ep1",
        rationale="updated rationale",
        proposed_changes=["new step"],
    ) is True
    reloaded = mem.get_plan("ep1")
    assert reloaded is not None
    assert reloaded.rationale == "updated rationale"
    assert reloaded.proposed_changes == ["new step"]
    # Untouched fields stay at the prior value.
    assert reloaded.acceptance_criteria == ["original criterion"]
    assert reloaded.files_affected == ["maestro/x.py"]
    # Status guard: still pending_review.
    assert reloaded.status == "pending_review"


def test_refine_plan_stamps_refined_at(mem_fs: dict[str, str]) -> None:
    """A successful refinement records refined_at; failures leave it None."""
    mem = _make_memory(mem_fs)
    plan = _sample_plan()
    mem.save_evolution_plan(plan)
    # Untouched plans have no refinement timestamp.
    before = mem.get_plan("ep1")
    assert before is not None
    assert before.refined_at is None

    assert mem.refine_plan("ep1", rationale="updated rationale") is True
    refined = mem.get_plan("ep1")
    assert refined is not None
    assert refined.refined_at is not None
    # ISO 8601 UTC timestamp.
    parsed = datetime.fromisoformat(refined.refined_at)
    assert parsed.tzinfo is not None
    first_stamp = refined.refined_at

    # A no-op refinement (unknown id) must not touch the existing stamp.
    assert mem.refine_plan("does-not-exist", rationale="X") is False
    still = mem.get_plan("ep1")
    assert still is not None
    assert still.refined_at == first_stamp


def test_refine_plan_updates_acceptance_criteria_in_isolation(
    mem_fs: dict[str, str],
) -> None:
    """refine_plan updates acceptance_criteria when passed alone."""
    mem = _make_memory(mem_fs)
    plan = _sample_plan()
    plan.rationale = "keep me"
    plan.proposed_changes = ["keep me too"]
    plan.acceptance_criteria = ["original criterion"]
    mem.save_evolution_plan(plan)

    assert mem.refine_plan(
        "ep1", acceptance_criteria=["sharper criterion"],
    ) is True
    reloaded = mem.get_plan("ep1")
    assert reloaded is not None
    assert reloaded.acceptance_criteria == ["sharper criterion"]
    assert reloaded.rationale == "keep me"
    assert reloaded.proposed_changes == ["keep me too"]


def test_refine_plan_rejects_acceptance_criterion_over_criterion_cap(
    mem_fs: dict[str, str],
) -> None:
    """#4104: an acceptance criterion is bound by the 200-char criterion cap,
    not the 1000-char proposed_changes cap; the change cap is unchanged."""
    from maestro.memory import _PLAN_CHANGE_ITEM_MAX_CHARS
    from maestro.types import ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS

    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())

    with pytest.raises(ValueError, match="acceptance_criteria items"):
        mem.refine_plan(
            "ep1",
            acceptance_criteria=["x" * (ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS + 1)],
        )

    at_cap = "y" * ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS
    change = "z" * _PLAN_CHANGE_ITEM_MAX_CHARS
    assert mem.refine_plan(
        "ep1", acceptance_criteria=[at_cap], proposed_changes=[change],
    ) is True
    reloaded = mem.get_plan("ep1")
    assert reloaded is not None
    assert reloaded.acceptance_criteria == [at_cap]
    assert reloaded.proposed_changes == [change]


def test_refine_plan_rejects_non_pending_plan(
    mem_fs: dict[str, str],
) -> None:
    """refine_plan returns False (no-op) for any non-pending_review status."""
    mem = _make_memory(mem_fs)
    plan = _sample_plan()
    plan.rationale = "original"
    mem.save_evolution_plan(plan)

    for terminal_status in ("approved", "rejected", "implemented", "merged",
                            "failed"):
        mem.update_plan_status("ep1", terminal_status)  # type: ignore[arg-type]
        assert mem.refine_plan("ep1", rationale="X") is False
        reloaded = mem.get_plan("ep1")
        assert reloaded is not None
        assert reloaded.rationale == "original"

    # ``obsolete`` uses its documented entry point — ``update_plan_status``
    # refuses to set it directly so ``obsolete_reason`` stays populated.
    mem.update_plan_status("ep1", "pending_review")
    assert mem.mark_plan_obsolete("ep1", "stale") is True
    assert mem.refine_plan("ep1", rationale="X") is False
    reloaded = mem.get_plan("ep1")
    assert reloaded is not None
    assert reloaded.rationale == "original"
    assert reloaded.status == "obsolete"


def test_refine_plan_returns_false_for_unknown_id(
    mem_fs: dict[str, str],
) -> None:
    """refine_plan returns False without raising when plan_id is missing."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    assert mem.refine_plan("does-not-exist", rationale="X") is False


def test_touch_plan_curated_stamps_last_curated_at(mem_fs: dict[str, str]) -> None:
    """A successful touch stamps last_curated_at without altering content or
    the content hash (the cursor is excluded from the hash, unlike refine)."""
    mem = _make_memory(mem_fs)
    plan = _sample_plan()
    plan.rationale = "unchanged"
    mem.save_evolution_plan(plan)
    before = mem.get_plan("ep1")
    assert before is not None
    assert before.last_curated_at is None
    hash_before = before.plan_content_hash

    assert mem.touch_plan_curated("ep1") is True
    touched = mem.get_plan("ep1")
    assert touched is not None
    assert touched.last_curated_at is not None
    # ISO 8601 UTC timestamp.
    parsed = datetime.fromisoformat(touched.last_curated_at)
    assert parsed.tzinfo is not None
    # Content and its hash are untouched — the cursor is not plan content.
    assert touched.rationale == "unchanged"
    assert touched.plan_content_hash == hash_before


def test_touch_plan_curated_rejects_non_pending_plan(
    mem_fs: dict[str, str],
) -> None:
    """touch_plan_curated returns False (no-op) for any non-pending status —
    a plan that already left pending_review is no longer curation-eligible."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())

    for terminal_status in ("approved", "rejected", "implemented", "merged",
                            "failed"):
        mem.update_plan_status("ep1", terminal_status)  # type: ignore[arg-type]
        assert mem.touch_plan_curated("ep1") is False
        reloaded = mem.get_plan("ep1")
        assert reloaded is not None
        assert reloaded.last_curated_at is None


def test_touch_plan_curated_returns_false_for_unknown_id(
    mem_fs: dict[str, str],
) -> None:
    """touch_plan_curated returns False without raising when plan_id is missing."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    assert mem.touch_plan_curated("does-not-exist") is False


def test_refine_plan_validates_field_types(mem_fs: dict[str, str]) -> None:
    """refine_plan type-validates rationale (str) and list fields (list[str])."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    with pytest.raises(TypeError, match="rationale"):
        mem.refine_plan("ep1", rationale=42)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="proposed_changes"):
        mem.refine_plan("ep1", proposed_changes="not a list")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="acceptance_criteria"):
        mem.refine_plan("ep1", acceptance_criteria=[1, 2])  # type: ignore[list-item]


def test_refine_plan_rejects_empty_values(mem_fs: dict[str, str]) -> None:
    """refine_plan rejects empty/whitespace strings and empty lists.

    Replacing rationale/proposed_changes/acceptance_criteria with an
    empty value is almost certainly an LLM bug — the curation pass is
    meant to *refresh* fields, not wipe them. Fail loud rather than
    silently corrupt the plan body.
    """
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    with pytest.raises(ValueError, match="rationale"):
        mem.refine_plan("ep1", rationale="")
    with pytest.raises(ValueError, match="rationale"):
        mem.refine_plan("ep1", rationale="   ")
    with pytest.raises(ValueError, match="proposed_changes"):
        mem.refine_plan("ep1", proposed_changes=[])
    with pytest.raises(ValueError, match="acceptance_criteria"):
        mem.refine_plan("ep1", acceptance_criteria=[])


def test_refine_plan_requires_at_least_one_field(
    mem_fs: dict[str, str],
) -> None:
    """refine_plan raises ValueError when no field is passed.

    A no-field call is a programmer/LLM mistake; without the guard the
    method would silently rewrite the JSONL without changing anything.
    """
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    with pytest.raises(ValueError, match="at least one"):
        mem.refine_plan("ep1")


def test_refine_plan_updates_all_three_fields_in_one_call(
    mem_fs: dict[str, str],
) -> None:
    """refine_plan applies rationale + proposed_changes + acceptance_criteria together."""
    mem = _make_memory(mem_fs)
    plan = _sample_plan()
    plan.rationale = "old rationale"
    plan.proposed_changes = ["old step"]
    plan.acceptance_criteria = ["old criterion"]
    mem.save_evolution_plan(plan)

    assert mem.refine_plan(
        "ep1",
        rationale="new rationale",
        proposed_changes=["new step a", "new step b"],
        acceptance_criteria=["new criterion"],
    ) is True
    reloaded = mem.get_plan("ep1")
    assert reloaded is not None
    assert reloaded.rationale == "new rationale"
    assert reloaded.proposed_changes == ["new step a", "new step b"]
    assert reloaded.acceptance_criteria == ["new criterion"]
    assert reloaded.status == "pending_review"


def test_refine_plan_rejects_oversized_rationale(mem_fs: dict[str, str]) -> None:
    """refine_plan caps rationale length so a confused LLM can't bloat the JSONL."""
    from maestro.memory import _PLAN_RATIONALE_MAX_CHARS

    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    too_long = "x" * (_PLAN_RATIONALE_MAX_CHARS + 1)
    with pytest.raises(ValueError, match="rationale exceeds"):
        mem.refine_plan("ep1", rationale=too_long)
    # Boundary: exactly at the cap is accepted.
    at_cap = "y" * _PLAN_RATIONALE_MAX_CHARS
    assert mem.refine_plan("ep1", rationale=at_cap) is True


def test_refine_plan_rejects_oversized_list_items(mem_fs: dict[str, str]) -> None:
    """refine_plan caps the length of each item in proposed_changes / acceptance_criteria."""
    from maestro.memory import _PLAN_CHANGE_ITEM_MAX_CHARS

    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    too_long = "x" * (_PLAN_CHANGE_ITEM_MAX_CHARS + 1)
    with pytest.raises(ValueError, match="proposed_changes items"):
        mem.refine_plan("ep1", proposed_changes=["ok", too_long])
    with pytest.raises(ValueError, match="acceptance_criteria items"):
        mem.refine_plan("ep1", acceptance_criteria=[too_long])


def test_refine_plan_rejects_over_count_acceptance_criteria(
    mem_fs: dict[str, str],
) -> None:
    """refine_plan caps acceptance_criteria at ``ACCEPTANCE_CRITERIA_MAX_ITEMS``.

    The write boundary inherited the type/non-empty/per-item-char validators
    but not the count cap the producers (propose_plan / task_generation)
    already enforce — so a curate ``refine`` verdict returning more than the cap
    used to persist verbatim, breaking the binding count contract and
    the context-bloat-cap invariant. The cap is now enforced loudly here.
    proposed_changes carries no such count cap, so it is unaffected.
    """
    from maestro.types import ACCEPTANCE_CRITERIA_MAX_ITEMS

    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    over_count = [f"criterion {i}" for i in range(ACCEPTANCE_CRITERIA_MAX_ITEMS + 1)]
    with pytest.raises(ValueError, match="acceptance_criteria"):
        mem.refine_plan("ep1", acceptance_criteria=over_count)
    # The inclusive upper boundary (exactly the cap) is accepted.
    at_cap = [f"criterion {i}" for i in range(ACCEPTANCE_CRITERIA_MAX_ITEMS)]
    assert mem.refine_plan("ep1", acceptance_criteria=at_cap) is True
    # proposed_changes has no count cap — a longer list is accepted.
    assert (
        mem.refine_plan(
            "ep1",
            proposed_changes=[f"change {i}" for i in range(ACCEPTANCE_CRITERIA_MAX_ITEMS + 3)],
        )
        is True
    )


def test_validate_acceptance_criteria_count_boundary() -> None:
    """The shared validator accepts at-cap and raises at cap+1.

    One owner for the documented 1-5 bound, so the two write doors and the
    prompt renderer cannot drift. The message names the field, the observed
    count, and the cap.
    """
    from maestro.types import (
        ACCEPTANCE_CRITERIA_MAX_ITEMS,
        validate_acceptance_criteria_count,
    )

    at_cap = [f"criterion {i}" for i in range(ACCEPTANCE_CRITERIA_MAX_ITEMS)]
    # At the inclusive upper boundary the validator is silent.
    assert validate_acceptance_criteria_count(at_cap) is None
    # An empty list carries no count violation — non-emptiness is a separate
    # concern owned by refine_plan.
    assert validate_acceptance_criteria_count([]) is None

    over_cap = [*at_cap, "one too many"]
    with pytest.raises(ValueError) as excinfo:
        validate_acceptance_criteria_count(over_cap)
    message = str(excinfo.value)
    assert "acceptance_criteria" in message
    assert str(len(over_cap)) in message
    assert str(ACCEPTANCE_CRITERIA_MAX_ITEMS) in message

    # The caller may name the field it is validating.
    with pytest.raises(ValueError, match="refined_criteria"):
        validate_acceptance_criteria_count(over_cap, field_name="refined_criteria")


def test_save_evolution_plan_rejects_over_count_acceptance_criteria(
    mem_fs: dict[str, str],
) -> None:
    """The low-level creation door enforces the same 1-5 count cap.

    ``save_evolution_plan`` is the primitive every internal mint shares, so a
    row admitted here survives every restart and flows into all four prompt
    renderings. Enforcing at ingress is what makes "no new over-cap row can be
    minted" true regardless of which caller bypassed the producer slice.
    """
    from maestro.types import ACCEPTANCE_CRITERIA_MAX_ITEMS

    mem = _make_memory(mem_fs)
    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")

    over_cap = _sample_plan()
    over_cap.acceptance_criteria = [
        f"criterion {i}" for i in range(ACCEPTANCE_CRITERIA_MAX_ITEMS + 1)
    ]
    with pytest.raises(ValueError, match="acceptance_criteria"):
        mem.save_evolution_plan(over_cap)
    # The refusal precedes the write — no partial row reaches the store.
    assert plans_path not in mem_fs

    # Exactly at the cap is admitted and persisted whole.
    at_cap = _sample_plan("ep-at-cap")
    at_cap.acceptance_criteria = [
        f"criterion {i}" for i in range(ACCEPTANCE_CRITERIA_MAX_ITEMS)
    ]
    assert mem.save_evolution_plan(at_cap) is None
    reloaded = mem.get_plan("ep-at-cap")
    assert reloaded is not None
    assert len(reloaded.acceptance_criteria) == ACCEPTANCE_CRITERIA_MAX_ITEMS


def test_parse_plan_line_warns_on_legacy_over_cap_criteria(
    mem_fs: dict[str, str], caplog: Any,
) -> None:
    """A legacy over-cap row loads intact and is surfaced as one WARNING.

    Rows written before the boundary raises landed can carry more than the
    cap. Truncating at the read door would silently rewrite an
    already-reviewed contract and desynchronise ``plan_content_hash``, so the
    loader keeps every criterion and names the offender instead; the prompt
    renderer is the choke that keeps the list out of context.
    """
    import logging

    from maestro import memory as memory_mod
    from maestro.types import ACCEPTANCE_CRITERIA_MAX_ITEMS

    mem = _make_memory(mem_fs)
    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    legacy = asdict(_sample_plan("ep-legacy"))
    over = ACCEPTANCE_CRITERIA_MAX_ITEMS + 3
    legacy["acceptance_criteria"] = [f"criterion {i}" for i in range(over)]
    mem_fs[plans_path] = json.dumps(legacy) + "\n"
    # The WARNING is deduped per plan_id for the life of the process, so clear
    # any prior sighting rather than depending on test execution order.
    memory_mod._WARNED_OVER_CAP_PLAN_IDS.discard("ep-legacy")

    with caplog.at_level(logging.WARNING):
        reloaded = mem.get_plan("ep-legacy")

    assert reloaded is not None
    # Loaded intact — every criterion survives, none truncated to the cap.
    assert len(reloaded.acceptance_criteria) == over
    assert reloaded.acceptance_criteria[-1] == f"criterion {over - 1}"
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "ep-legacy" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert str(over) in warnings[0].getMessage()
    assert str(ACCEPTANCE_CRITERIA_MAX_ITEMS) in warnings[0].getMessage()


def test_parse_plan_line_warns_once_per_plan_id(
    mem_fs: dict[str, str], caplog: Any,
) -> None:
    """The legacy-row WARNING is deduped per ``plan_id`` per process.

    ``_parse_plan_line`` runs on every line of every load, many times per loop
    iteration; an undeduped warning would drown the signal it exists to carry.
    """
    import logging

    from maestro import memory as memory_mod
    from maestro.types import ACCEPTANCE_CRITERIA_MAX_ITEMS

    mem = _make_memory(mem_fs)
    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    legacy = asdict(_sample_plan("ep-noisy"))
    legacy["acceptance_criteria"] = [f"criterion {i}" for i in range(ACCEPTANCE_CRITERIA_MAX_ITEMS + 3)]
    mem_fs[plans_path] = json.dumps(legacy) + "\n"
    memory_mod._WARNED_OVER_CAP_PLAN_IDS.discard("ep-noisy")

    try:
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                assert mem.get_plan("ep-noisy") is not None
        warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "ep-noisy" in r.getMessage()
        ]
        assert len(warnings) == 1
    finally:
        memory_mod._WARNED_OVER_CAP_PLAN_IDS.discard("ep-noisy")


def test_parse_plan_line_silent_on_at_cap_criteria(
    mem_fs: dict[str, str], caplog: Any,
) -> None:
    """An at-cap row loads with no WARNING — the bound is inclusive."""
    import logging

    from maestro.types import ACCEPTANCE_CRITERIA_MAX_ITEMS

    mem = _make_memory(mem_fs)
    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    row = asdict(_sample_plan("ep-at-cap"))
    row["acceptance_criteria"] = [
        f"criterion {i}" for i in range(ACCEPTANCE_CRITERIA_MAX_ITEMS)
    ]
    mem_fs[plans_path] = json.dumps(row) + "\n"

    with caplog.at_level(logging.WARNING):
        reloaded = mem.get_plan("ep-at-cap")

    assert reloaded is not None
    assert len(reloaded.acceptance_criteria) == ACCEPTANCE_CRITERIA_MAX_ITEMS
    assert [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "acceptance_criteria" in r.getMessage()
    ] == []


def test_mark_plan_obsolete_transitions_pending_plan(
    mem_fs: dict[str, str],
) -> None:
    """mark_plan_obsolete sets status=obsolete + obsolete_reason + reviewed_at."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    assert mem.mark_plan_obsolete("ep1", "superseded by ep2") is True
    reloaded = mem.get_plan("ep1")
    assert reloaded is not None
    assert reloaded.status == "obsolete"
    assert reloaded.obsolete_reason == "superseded by ep2"
    assert reloaded.reviewed_at is not None
    # Self-pruning must NOT populate rejection_reason — that channel
    # feeds learn_from_rejection and would mis-classify the prune.
    assert reloaded.rejection_reason is None


def test_mark_plan_obsolete_functions_while_backlog_saturated(
    mem_fs: dict[str, str],
) -> None:
    """The owned disposal lever never depends on backlog size.

    The submit-time production cap (``pending_plan_submit_block_threshold``)
    binds only ``Evolution.submit_for_review``; ``mark_plan_obsolete`` — the
    disposal lever the cap intends the forward path to use — must keep
    functioning no matter how many ``pending_review`` plans already exist.
    """
    mem = _make_memory(mem_fs)
    for i in range(25):
        mem.save_evolution_plan(_plan(f"sat{i}", f"Seed {i}", f"body {i}"))
    assert len(mem.get_pending_plans()) == 25

    assert mem.mark_plan_obsolete("sat0", "disposed under saturation") is True
    reloaded = mem.get_plan("sat0")
    assert reloaded is not None
    assert reloaded.status == "obsolete"
    assert len(mem.get_pending_plans()) == 24


def test_mark_plan_obsolete_rejects_non_pending_plan(
    mem_fs: dict[str, str],
) -> None:
    """mark_plan_obsolete is a no-op (returns False) outside pending_review."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    for terminal_status in ("approved", "rejected", "implemented", "merged",
                            "failed"):
        mem.update_plan_status("ep1", terminal_status)  # type: ignore[arg-type]
        assert mem.mark_plan_obsolete("ep1", "stale") is False
        reloaded = mem.get_plan("ep1")
        assert reloaded is not None
        assert reloaded.status == terminal_status
        assert reloaded.obsolete_reason is None

    # Reset to pending_review, then enter the obsolete state via the
    # documented entry point so the fixture matches the dataclass-field
    # invariant ("Populated only when status == 'obsolete'"). A second
    # mark_plan_obsolete call must then be a no-op that preserves both
    # the original reason and the obsolete status.
    mem.update_plan_status("ep1", "pending_review")
    assert mem.mark_plan_obsolete("ep1", "original reason") is True
    assert mem.mark_plan_obsolete("ep1", "second reason") is False
    reloaded = mem.get_plan("ep1")
    assert reloaded is not None
    assert reloaded.status == "obsolete"
    assert reloaded.obsolete_reason == "original reason"


def test_mark_plan_obsolete_implemented_refused_by_default(
    mem_fs: dict[str, str],
) -> None:
    """Without the opt-in, an implemented plan is refused — the starved lever."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    mem.update_plan_status("ep1", "implemented")
    assert mem.mark_plan_obsolete("ep1", "stale") is False
    reloaded = mem.get_plan("ep1")
    assert reloaded is not None
    assert reloaded.status == "implemented"
    assert reloaded.obsolete_reason is None


def test_mark_plan_obsolete_allow_implemented_flips_plan(
    mem_fs: dict[str, str],
) -> None:
    """With allow_implemented=True an implemented plan transitions to obsolete."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    mem.update_plan_status("ep1", "implemented")
    assert mem.mark_plan_obsolete(
        "ep1", "dead cure", allow_implemented=True,
    ) is True
    reloaded = mem.get_plan("ep1")
    assert reloaded is not None
    assert reloaded.status == "obsolete"
    assert reloaded.obsolete_reason == "dead cure"
    assert reloaded.reviewed_at is not None
    # Self-pruning must NOT populate rejection_reason — that channel
    # feeds learn_from_rejection and would mis-classify the prune.
    assert reloaded.rejection_reason is None


def test_mark_plan_obsolete_allow_implemented_still_refuses_others(
    mem_fs: dict[str, str],
) -> None:
    """The opt-in widens to implemented only — other statuses stay refused."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    for terminal_status in ("approved", "rejected", "merged", "failed"):
        mem.update_plan_status("ep1", terminal_status)  # type: ignore[arg-type]
        assert mem.mark_plan_obsolete(
            "ep1", "stale", allow_implemented=True,
        ) is False
        reloaded = mem.get_plan("ep1")
        assert reloaded is not None
        assert reloaded.status == terminal_status
        assert reloaded.obsolete_reason is None


def test_mark_plan_obsolete_allow_approved_flips_plan(
    mem_fs: dict[str, str],
) -> None:
    """With allow_approved=True an approved plan transitions to obsolete.

    Proposal 118's scope-rejection slicing path retires the superseded
    original — an ``approved`` plan whose too-large intent became an
    ``EvolutionProposal`` chain — as ``obsolete`` (self-prune), not
    ``failed`` (demotion / rejection learning).
    """
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    mem.update_plan_status("ep1", "approved")
    assert mem.mark_plan_obsolete(
        "ep1", "sliced into EvolutionProposal chain", allow_approved=True,
    ) is True
    reloaded = mem.get_plan("ep1")
    assert reloaded is not None
    assert reloaded.status == "obsolete"
    assert reloaded.obsolete_reason == "sliced into EvolutionProposal chain"
    assert reloaded.reviewed_at is not None
    # Self-pruning must NOT populate rejection_reason — a sliced plan is
    # superseded, never rejected; the rejection-learning cycle must not see it.
    assert reloaded.rejection_reason is None


def test_mark_plan_obsolete_allow_approved_still_refuses_others(
    mem_fs: dict[str, str],
) -> None:
    """The opt-in widens to approved only — other statuses stay refused.

    In particular ``implemented`` remains refused unless its own
    ``allow_implemented`` opt-in is set, so the two opt-ins stay orthogonal.
    """
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    for terminal_status in ("rejected", "merged", "failed", "implemented"):
        mem.update_plan_status("ep1", terminal_status)  # type: ignore[arg-type]
        assert mem.mark_plan_obsolete(
            "ep1", "stale", allow_approved=True,
        ) is False
        reloaded = mem.get_plan("ep1")
        assert reloaded is not None
        assert reloaded.status == terminal_status
        assert reloaded.obsolete_reason is None


def test_mark_plan_obsolete_allow_failed_flips_plan(
    mem_fs: dict[str, str],
) -> None:
    """With allow_failed=True a failed plan transitions to obsolete (#4123)."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    mem.update_plan_status("ep1", "failed")
    assert mem.mark_plan_obsolete(
        "ep1", "over-cap criteria", allow_failed=True,
    ) is True
    reloaded = mem.get_plan("ep1")
    assert reloaded is not None
    assert reloaded.status == "obsolete"
    assert reloaded.obsolete_reason == "over-cap criteria"
    assert reloaded.rejection_reason is None


def test_mark_plan_obsolete_allow_failed_still_refuses_others(
    mem_fs: dict[str, str],
) -> None:
    """Without the opt-in a failed plan is refused; with it, others still are."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    mem.update_plan_status("ep1", "failed")
    assert mem.mark_plan_obsolete("ep1", "stale") is False
    reloaded = mem.get_plan("ep1")
    assert reloaded is not None
    assert reloaded.status == "failed"
    for other_status in ("rejected", "merged", "implemented", "approved"):
        mem.update_plan_status("ep1", other_status)  # type: ignore[arg-type]
        assert mem.mark_plan_obsolete(
            "ep1", "stale", allow_failed=True,
        ) is False
        reloaded = mem.get_plan("ep1")
        assert reloaded is not None
        assert reloaded.status == other_status


def test_mark_plan_obsolete_returns_false_for_unknown_id(
    mem_fs: dict[str, str],
) -> None:
    """mark_plan_obsolete returns False without raising when plan_id is missing."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    assert mem.mark_plan_obsolete("nope", "stale") is False


def test_mark_plan_obsolete_validates_reason_type(
    mem_fs: dict[str, str],
) -> None:
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    with pytest.raises(TypeError, match="reason"):
        mem.mark_plan_obsolete("ep1", 42)  # type: ignore[arg-type]


def test_mark_plan_obsolete_rejects_empty_reason(
    mem_fs: dict[str, str],
) -> None:
    """mark_plan_obsolete rejects empty/whitespace reasons.

    An empty ``obsolete_reason`` is indistinguishable from a legacy
    plan that was never self-pruned, breaking the audit trail.
    """
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    with pytest.raises(ValueError, match="reason"):
        mem.mark_plan_obsolete("ep1", "")
    with pytest.raises(ValueError, match="reason"):
        mem.mark_plan_obsolete("ep1", "   ")


def test_update_plan_status_rejects_obsolete(
    mem_fs: dict[str, str],
) -> None:
    """update_plan_status refuses ``"obsolete"`` — callers must use mark_plan_obsolete.

    Without this guard a caller could leave ``obsolete_reason=None``
    while flipping ``status``, silently breaking the audit trail the
    dashboard renders from ``obsolete_reason``.
    """
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    with pytest.raises(ValueError, match="mark_plan_obsolete"):
        mem.update_plan_status("ep1", "obsolete")  # type: ignore[arg-type]
    # Plan state must not have changed.
    reloaded = mem.get_plan("ep1")
    assert reloaded is not None
    assert reloaded.status == "pending_review"
    assert reloaded.obsolete_reason is None


def test_mark_plan_obsolete_rejects_oversized_reason(
    mem_fs: dict[str, str],
) -> None:
    """mark_plan_obsolete caps reason length (one-line audit text)."""
    from maestro.memory import _OBSOLETE_REASON_MAX_CHARS

    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    too_long = "x" * (_OBSOLETE_REASON_MAX_CHARS + 1)
    with pytest.raises(ValueError, match="exceeds"):
        mem.mark_plan_obsolete("ep1", too_long)
    # Boundary: exactly at the cap is accepted.
    at_cap = "y" * _OBSOLETE_REASON_MAX_CHARS
    assert mem.mark_plan_obsolete("ep1", at_cap) is True


def test_old_plan_jsonl_without_new_fields_loads(mem_fs: dict[str, str]) -> None:
    """Legacy JSONL without new fields loads with None defaults."""
    mem = _make_memory(mem_fs)
    old_plan = {
        "plan_id": "ep_old",
        "title": "legacy",
        "stage": "awakening",
        "category": "capability",
        "rationale": "r",
        "proposed_changes": [],
        "files_affected": [],
        "expected_impact": "",
        "priority": "capability",
        "status": "implemented",
        "created_at": "2024-01-01T00:00:00Z",
    }
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = (
        json.dumps(old_plan) + "\n"
    )

    loaded = mem.get_plan("ep_old")
    assert loaded is not None
    assert loaded.last_pr_reminder_at is None
    assert loaded.last_conflict_resolution_attempt_at is None
    assert loaded.obsolete_reason is None
    # Proposal 124 hold fields absent on the legacy row default None / 0.
    assert loaded.inherited_hold_main_sha is None
    assert loaded.inherited_hold_count == 0
    # Proposal 133's environment hold likewise defaults on a legacy row.
    assert loaded.environment_hold_fingerprint is None


def test_load_plan_without_source_task_id_field(mem_fs: dict[str, str]) -> None:
    """Legacy JSONL row without source_task_id loads with the field defaulting to None.

    Backwards-compat is automatic because ``_parse_plan_line`` filters JSON
    keys against the ``EvolutionPlan`` field set, so missing optional fields
    fall through to dataclass defaults.
    """
    mem = _make_memory(mem_fs)
    legacy_plan = {
        "plan_id": "ep_no_src",
        "title": "legacy",
        "stage": "awakening",
        "category": "capability",
        "rationale": "r",
        "proposed_changes": [],
        "files_affected": [],
        "expected_impact": "",
        "priority": "capability",
        "status": "implemented",
        "created_at": "2024-01-01T00:00:00Z",
    }
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = (
        json.dumps(legacy_plan) + "\n"
    )

    loaded = mem.get_plan("ep_no_src")
    assert loaded is not None
    assert loaded.source_task_id is None


def test_acceptance_criteria_round_trip(mem_fs: dict[str, str]) -> None:
    """An EvolutionPlan with acceptance_criteria persists through save+reload."""
    mem = _make_memory(mem_fs)
    plan = EvolutionPlan(
        plan_id="ep_ac",
        title="binding",
        category="capability",
        rationale="r",
        proposed_changes=["change a"],
        files_affected=["f.py"],
        expected_impact="",
        acceptance_criteria=["criterion one", "criterion two"],
        created_at="2026-05-01T00:00:00Z",
    )
    mem.save_evolution_plan(plan)

    loaded = mem.get_plan("ep_ac")
    assert loaded is not None
    assert loaded.acceptance_criteria == ["criterion one", "criterion two"]


def test_load_plan_without_acceptance_criteria_field(
    mem_fs: dict[str, str],
) -> None:
    """Legacy JSONL row predating the acceptance_criteria field loads with []."""
    mem = _make_memory(mem_fs)
    legacy_plan = {
        "plan_id": "ep_no_ac",
        "title": "legacy",
        "stage": "awakening",
        "category": "capability",
        "rationale": "r",
        "proposed_changes": [],
        "files_affected": [],
        "expected_impact": "",
        "priority": "capability",
        "status": "implemented",
        "created_at": "2024-01-01T00:00:00Z",
    }
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = (
        json.dumps(legacy_plan) + "\n"
    )

    loaded = mem.get_plan("ep_no_ac")
    assert loaded is not None
    assert loaded.acceptance_criteria == []


def test_load_plan_acceptance_criteria_drops_non_string_items(
    mem_fs: dict[str, str],
) -> None:
    """``_parse_plan_line``'s str-list coercion drops non-string entries from
    a corrupt acceptance_criteria array (None, ints, dicts) instead of
    crashing the loader."""
    mem = _make_memory(mem_fs)
    corrupt_plan = {
        "plan_id": "ep_corrupt",
        "title": "corrupt",
        "stage": "awakening",
        "category": "capability",
        "rationale": "r",
        "proposed_changes": [],
        "files_affected": [],
        "expected_impact": "",
        "priority": "capability",
        "status": "implemented",
        "created_at": "2024-01-01T00:00:00Z",
        "acceptance_criteria": ["valid", None, 42, {"x": 1}, "also valid"],
    }
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = (
        json.dumps(corrupt_plan) + "\n"
    )

    loaded = mem.get_plan("ep_corrupt")
    assert loaded is not None
    assert loaded.acceptance_criteria == ["valid", "also valid"]


def test_get_task_record_returns_match(mem_fs: dict[str, str]) -> None:
    """``get_task_record`` returns the latest record matching ``task_id``."""
    mem = _make_memory(mem_fs)
    older = TaskRecord(
        task_id="t1", goal="g1", plan=[], outcome="failure",
        result_summary="r1", errors=["boom"], duration_secs=1.0,
        timestamp="2026-01-01", branch=None,
    )
    newer = TaskRecord(
        task_id="t1", goal="g1-retry", plan=[], outcome="success",
        result_summary="r2", errors=[], duration_secs=2.0,
        timestamp="2026-01-02", branch="maestro/task/t1",
    )
    other = TaskRecord(
        task_id="t2", goal="g2", plan=[], outcome="success",
        result_summary="r3", errors=[], duration_secs=1.5,
        timestamp="2026-01-03",
    )
    mem.record_task(older)
    mem.record_task(newer)
    mem.record_task(other)

    found = mem.get_task_record("t1")
    assert found is not None
    assert found.outcome == "success"
    assert found.branch == "maestro/task/t1"


def test_get_task_record_missing_returns_none(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    assert mem.get_task_record("does_not_exist") is None


def test_get_task_record_skips_non_dict_json_rows(
    mem_fs: dict[str, str],
) -> None:
    """A row whose JSON parses to a non-dict (e.g. a list) must be skipped
    rather than crashing the lookup."""
    mem = _make_memory(mem_fs)
    valid = json.dumps({
        "task_id": "ok", "goal": "g", "plan": [],
        "outcome": "success", "result_summary": "", "errors": [],
        "duration_secs": 1.0, "timestamp": "2026-01-01",
        "branch": "maestro/task/ok",
    })
    mem_fs[str(FAKE_DIR / "task_history.jsonl")] = (
        '["not", "a", "dict"]\n' + valid + "\n"
    )

    found = mem.get_task_record("ok")
    assert found is not None
    assert found.task_id == "ok"


def test_get_task_record_skips_blank_and_malformed_lines(
    mem_fs: dict[str, str],
) -> None:
    """``_parse_task_line`` must skip blank and JSON-decode-failing lines so
    ``get_task_record``'s linear scan never crashes on adjacent garbage."""
    mem = _make_memory(mem_fs)
    valid = json.dumps({
        "task_id": "ok", "goal": "g", "plan": [],
        "outcome": "success", "result_summary": "", "errors": [],
        "duration_secs": 1.0, "timestamp": "2026-01-01",
    })
    mem_fs[str(FAKE_DIR / "task_history.jsonl")] = (
        "\n"  # exercises blank-line return None
        "this is not json {{{\n"  # exercises JSONDecodeError
        f"{valid}\n"
    )

    found = mem.get_task_record("ok")
    assert found is not None
    assert found.task_id == "ok"
