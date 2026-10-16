"""Tests for the Telegram plan approve/reject decision helper.

All I/O runs through ``mem_fs``; no real filesystem, network, or sleep.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from maestro.memory import EvolutionPlan, Memory, compute_plan_content_hash
from maestro.notifications.base import PlanContentDriftError
from maestro.plan_decision import apply_telegram_plan_decision

FAKE_DIR = Path("/fake/decide")


def _memory_with_plan(
    plan_id: str, *, notified: bool = True, status: str = "pending_review",
) -> Memory:
    memory = Memory(FAKE_DIR)
    memory.save_evolution_plan(EvolutionPlan(
        plan_id=plan_id, title="Decide",
        category="capability", rationale="test", proposed_changes=["Change A"],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
        status=status,  # type: ignore[arg-type]
    ))
    if notified:
        saved = memory.get_plan(plan_id)
        assert saved is not None
        memory.update_plan_status(
            plan_id, status,  # type: ignore[arg-type]
            notified_content_hash=compute_plan_content_hash(saved),
        )
    return memory


def test_approve_records_approved(mem_fs: dict[str, str]) -> None:
    """A clean approve stamps ``approved`` and leaves no rejection reason."""
    memory = _memory_with_plan("pd_ok")

    assert apply_telegram_plan_decision(memory, "pd_ok", "approved") == "approved"

    plan = memory.get_plan("pd_ok")
    assert plan is not None
    assert plan.status == "approved"
    assert plan.rejection_reason is None
    assert plan.reviewed_at is not None


def test_reject_records_reason(mem_fs: dict[str, str]) -> None:
    """A clean reject stamps the supplied reason."""
    memory = _memory_with_plan("pd_rej")

    result = apply_telegram_plan_decision(
        memory, "pd_rej", "rejected", rejection_reason="Rejected via Telegram",
    )
    assert result == "rejected"

    plan = memory.get_plan("pd_rej")
    assert plan is not None
    assert plan.status == "rejected"
    assert plan.rejection_reason == "Rejected via Telegram"


def test_unnotified_plan_is_unconstrained(mem_fs: dict[str, str]) -> None:
    """An empty notified hash predates hash binding — nothing to compare."""
    memory = _memory_with_plan("pd_unnotified", notified=False)

    assert (
        apply_telegram_plan_decision(memory, "pd_unnotified", "approved")
        == "approved"
    )


def test_missing_plan_raises(mem_fs: dict[str, str]) -> None:
    """An unknown plan id refuses rather than writing anything."""
    memory = Memory(FAKE_DIR)

    with pytest.raises(ValueError, match="not in pending_review"):
        apply_telegram_plan_decision(memory, "pd_absent", "approved")


def test_non_pending_plan_raises(mem_fs: dict[str, str]) -> None:
    """A plan another surface already resolved refuses at read time."""
    memory = _memory_with_plan("pd_done", notified=False, status="approved")

    with pytest.raises(ValueError, match="not in pending_review"):
        apply_telegram_plan_decision(memory, "pd_done", "rejected")


def test_pre_lock_drift_raises(mem_fs: dict[str, str]) -> None:
    """Content that drifted before the call is caught by the cheap check."""
    memory = _memory_with_plan("pd_drift", notified=False)
    memory.update_plan_status(
        "pd_drift", "pending_review", notified_content_hash="0" * 64,
    )

    with pytest.raises(ValueError, match="content drifted"):
        apply_telegram_plan_decision(memory, "pd_drift", "approved")

    plan = memory.get_plan("pd_drift")
    assert plan is not None
    assert plan.status == "pending_review"


@pytest.mark.parametrize("prefix_len", [1, 15, 17])
def test_wrong_length_hash_prefix_is_drift(
    mem_fs: dict[str, str], prefix_len: int,
) -> None:
    """A truncated or overlong prefix never binds, even when it matches."""
    memory = _memory_with_plan("pd_short")
    plan = memory.get_plan("pd_short")
    assert plan is not None
    prefix = plan.plan_content_hash[:prefix_len] + (
        "0" if prefix_len > 16 else ""
    )

    with pytest.raises(PlanContentDriftError):
        apply_telegram_plan_decision(
            memory, "pd_short", "approved", rendered_hash_prefix=prefix,
        )

    after = memory.get_plan("pd_short")
    assert after is not None
    assert after.status == "pending_review"


def test_status_loss_returns_ignored(mem_fs: dict[str, str]) -> None:
    """A rival surface resolving the plan mid-write yields ``ignored``."""
    memory = _memory_with_plan("pd_race", notified=False)

    real_update = memory.update_plan_status
    calls = {"n": 0}

    def racy_update(*args: Any, **kwargs: Any) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            real_update("pd_race", "approved", reviewed_at="2026-01-01T00:00:00Z")
        return real_update(*args, **kwargs)

    with patch.object(memory, "update_plan_status", side_effect=racy_update):
        result = apply_telegram_plan_decision(
            memory, "pd_race", "rejected", rejection_reason="Rejected via Telegram",
        )
    assert result == "ignored"

    plan = memory.get_plan("pd_race")
    assert plan is not None
    assert plan.status == "approved"
    assert plan.rejection_reason is None


def test_deleted_plan_mid_write_returns_ignored(mem_fs: dict[str, str]) -> None:
    """A plan that vanishes between the read and the write yields ``ignored``."""
    memory = _memory_with_plan("pd_gone", notified=False)

    with (
        patch.object(memory, "update_plan_status", return_value=False),
        patch.object(memory, "get_plan", side_effect=[
            memory.get_plan("pd_gone"), None,
        ]),
    ):
        assert (
            apply_telegram_plan_decision(memory, "pd_gone", "approved") == "ignored"
        )


@pytest.mark.parametrize(
    ("status", "reason"),
    [("approved", None), ("rejected", "Rejected via Telegram")],
)
def test_refine_race_refuses(
    mem_fs: dict[str, str], status: str, reason: str | None,
) -> None:
    """A concurrent refine_plan rewrites the body after the cheap check.

    ``refine_plan`` deliberately leaves the plan ``pending_review``, so the
    status precondition still passes; only ``expect_content_hash`` catches
    the loss, and the decision must refuse rather than land on content the
    human never saw (**Plan content-hash binding**).
    """
    plan_id = f"pd_refine_{status}"
    memory = _memory_with_plan(plan_id)

    real_update = memory.update_plan_status
    calls = {"n": 0}

    def racy_update(*args: Any, **kwargs: Any) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            memory.refine_plan(plan_id, proposed_changes=["Change B"])
        return real_update(*args, **kwargs)

    with (
        patch.object(memory, "update_plan_status", side_effect=racy_update),
        pytest.raises(ValueError, match="content drifted"),
    ):
        apply_telegram_plan_decision(
            memory, plan_id, status, rejection_reason=reason,  # type: ignore[arg-type]
        )

    plan = memory.get_plan(plan_id)
    assert plan is not None
    assert plan.status == "pending_review"
    assert plan.rejection_reason is None
    assert plan.reviewed_at is None
    assert plan.proposed_changes == ["Change B"]


# ------------------------------------------------------------------
# Per-button snapshot binding (#4146)
# ------------------------------------------------------------------


def _refine(memory: Memory, plan_id: str) -> str:
    """Refine *plan_id* and re-stamp the notified hash, as curate does."""
    assert memory.refine_plan(plan_id, proposed_changes=["Change B"])
    refined = memory.get_plan(plan_id)
    assert refined is not None
    memory.update_plan_status(
        plan_id, "pending_review",
        notified_content_hash=refined.plan_content_hash,
    )
    return refined.plan_content_hash


def _hash_of(memory: Memory, plan_id: str) -> str:
    plan = memory.get_plan(plan_id)
    assert plan is not None
    return compute_plan_content_hash(plan)


@pytest.mark.parametrize(
    ("status", "reason"),
    [("approved", None), ("rejected", "Rejected via Telegram")],
)
def test_stale_button_refused_after_restamp(
    mem_fs: dict[str, str], status: str, reason: str | None,
) -> None:
    """A pre-refine button is refused even though the row was re-stamped."""
    plan_id = f"pd_stale_{status}"
    memory = _memory_with_plan(plan_id)
    v1_prefix = _hash_of(memory, plan_id)[:16]
    _refine(memory, plan_id)

    with pytest.raises(ValueError, match="content drifted"):
        apply_telegram_plan_decision(
            memory, plan_id, status,  # type: ignore[arg-type]
            rejection_reason=reason, rendered_hash_prefix=v1_prefix,
        )

    plan = memory.get_plan(plan_id)
    assert plan is not None
    assert plan.status == "pending_review"
    assert plan.reviewed_at is None
    assert plan.rejection_reason is None


def test_current_button_lands_after_refine(mem_fs: dict[str, str]) -> None:
    """The refined message's own button approves the refined body."""
    memory = _memory_with_plan("pd_v2")
    v2_hash = _refine(memory, "pd_v2")

    result = apply_telegram_plan_decision(
        memory, "pd_v2", "approved", rendered_hash_prefix=v2_hash[:16],
    )

    assert result == "approved"


def test_legacy_button_refused_on_refined_plan(mem_fs: dict[str, str]) -> None:
    """A hash-less button cannot vouch for a plan that has had two bodies."""
    memory = _memory_with_plan("pd_legacy_ref")
    _refine(memory, "pd_legacy_ref")

    with pytest.raises(ValueError, match="content drifted"):
        apply_telegram_plan_decision(memory, "pd_legacy_ref", "approved")

    plan = memory.get_plan("pd_legacy_ref")
    assert plan is not None
    assert plan.status == "pending_review"


def test_legacy_button_accepted_on_unrefined_plan(mem_fs: dict[str, str]) -> None:
    """A hash-less button on a never-refined plan keeps today's check."""
    memory = _memory_with_plan("pd_legacy_ok")

    result = apply_telegram_plan_decision(memory, "pd_legacy_ok", "approved")

    assert result == "approved"


def test_button_refine_race_refuses(mem_fs: dict[str, str]) -> None:
    """A refine landing after the prefix check is caught under the lock."""
    memory = _memory_with_plan("pd_btn_race")
    prefix = _hash_of(memory, "pd_btn_race")[:16]
    real_update = memory.update_plan_status
    calls = {"n": 0}

    def racy_update(*args: Any, **kwargs: Any) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            memory.refine_plan("pd_btn_race", proposed_changes=["Change B"])
        return real_update(*args, **kwargs)

    with (
        patch.object(memory, "update_plan_status", side_effect=racy_update),
        pytest.raises(ValueError, match="content drifted"),
    ):
        apply_telegram_plan_decision(
            memory, "pd_btn_race", "approved", rendered_hash_prefix=prefix,
        )

    plan = memory.get_plan("pd_btn_race")
    assert plan is not None
    assert plan.status == "pending_review"
