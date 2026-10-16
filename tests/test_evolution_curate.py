"""curate_pending_plans (proposal 060); split out of ``tests/test_evolution.py``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.evolution import (
    EvolutionEngine,
)
from maestro.issues import IssueTracker
from maestro.journal import Journal
from maestro.memory import (
    _PLAN_FILE_PATH_MAX_CHARS,
    _PLAN_FILES_AFFECTED_MAX_ITEMS,
    Memory,
)
from tests.conftest import MockQueryResult
from tests.test_evolution import (
    _make_curate_engine,
    _seed_pending_plan,
)


@pytest.mark.asyncio
async def test_curate_no_pending_plan_returns_false_no_sdk(
    mem_fs: dict[str, str],
) -> None:
    """No pending plan ⇒ early-return False without any SDK call."""
    e, _m, notifier = _make_curate_engine(mem_fs)
    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        result = await e.curate_pending_plans()
    assert result is False
    mock_q.assert_not_called()
    notifier.notify_plan_refined.assert_not_awaited()


@pytest.mark.asyncio
async def test_curate_too_young_plan_returns_false_no_sdk(
    mem_fs: dict[str, str],
) -> None:
    """A pending plan younger than the ``curate_min_plan_age_hours`` grace
    period is dropped by the selector ⇒ early-return False, no SDK call."""
    from datetime import UTC, datetime, timedelta

    e, m, notifier = _make_curate_engine(mem_fs)
    e._config.loop_curate_min_plan_age_hours = 24.0
    young_created = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    _seed_pending_plan(m, created_at=young_created)
    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        result = await e.curate_pending_plans()
    assert result is False
    mock_q.assert_not_called()
    notifier.notify_plan_refined.assert_not_awaited()


@pytest.mark.asyncio
async def test_curate_aged_plan_passes_grace_gate_and_calls_sdk(
    mem_fs: dict[str, str],
) -> None:
    """With a positive grace period, a plan older than the floor stays
    eligible: the engine proceeds to the SDK (a ``keep`` verdict is a clean
    no-op), confirming the gate does not block a mature backlog."""
    from datetime import UTC, datetime, timedelta

    e, m, _notifier = _make_curate_engine(mem_fs)
    e._config.loop_curate_min_plan_age_hours = 24.0
    aged_created = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
    _seed_pending_plan(m, created_at=aged_created)
    with patch(
        "maestro.evolution.query",
        AsyncMock(
            return_value=MockQueryResult(response=json.dumps({"action": "keep"})),
        ),
    ) as mock_q:
        result = await e.curate_pending_plans()
    assert result is False
    mock_q.assert_awaited_once()


@pytest.mark.asyncio
async def test_curate_refine_updates_only_changed_fields_and_notifies(
    mem_fs: dict[str, str],
) -> None:
    """A ``refine`` verdict rewrites exactly the returned fields, stamps
    ``refined_at``, and fires one refined-notification."""
    e, m, notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m)
    new_criteria = ["A unit test asserts foo() returns the cached value on call 2."]
    verdict = json.dumps({"action": "refine", "acceptance_criteria": new_criteria})

    with patch.object(
        e._memory, "refine_plan", wraps=e._memory.refine_plan,
    ) as spy, patch(
        "maestro.evolution.query",
        AsyncMock(return_value=MockQueryResult(response=verdict)),
    ):
        result = await e.curate_pending_plans()

    assert result is True
    # refine_plan called with exactly the changed field — nothing else.
    assert spy.call_args.args == ("cp1",)
    assert spy.call_args.kwargs == {"acceptance_criteria": new_criteria}
    updated = m.get_plan("cp1")
    assert updated is not None
    assert updated.acceptance_criteria == new_criteria
    # Untouched fields are preserved.
    assert updated.rationale == "Repeated fragments recompute every call."
    assert updated.proposed_changes == ["Hash fragments", "Reuse on hit"]
    assert updated.refined_at is not None
    assert updated.status == "pending_review"
    notifier.notify_plan_refined.assert_awaited_once()
    assert notifier.notify_plan_refined.await_args.args[0] == "cp1"


@pytest.mark.asyncio
async def test_curate_refine_session_path_uses_query_on_session(
    mem_fs: dict[str, str],
) -> None:
    """The session path dispatches on ``query_on_session`` with json_complete."""
    from maestro.sdk import json_complete

    e, m, notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m)
    verdict = json.dumps(
        {"action": "refine", "rationale": "Sharper rationale for the human reviewer."},
    )
    session = MagicMock()
    with patch(
        "maestro.evolution.query_on_session",
        new_callable=AsyncMock,
    ) as mock_qos:
        mock_qos.return_value = MockQueryResult(response=verdict)
        result = await e.curate_pending_plans(session=session)

    assert result is True
    mock_qos.assert_awaited_once()
    assert mock_qos.call_args.kwargs.get("complete_on") is json_complete
    assert m.get_plan("cp1").rationale == "Sharper rationale for the human reviewer."  # type: ignore[union-attr]
    notifier.notify_plan_refined.assert_awaited_once()


@pytest.mark.asyncio
async def test_curate_obsolete_marks_plan_and_does_not_notify_as_refined(
    mem_fs: dict[str, str],
) -> None:
    """An ``obsolete`` verdict self-prunes and sends no refined-notification."""
    e, m, notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m)
    verdict = json.dumps(
        {"action": "obsolete", "reason": "Superseded by the merged cache layer."},
    )
    with patch(
        "maestro.evolution.query",
        AsyncMock(return_value=MockQueryResult(response=verdict)),
    ):
        result = await e.curate_pending_plans()

    assert result is True
    updated = m.get_plan("cp1")
    assert updated is not None
    assert updated.status == "obsolete"
    assert updated.obsolete_reason == "Superseded by the merged cache layer."
    assert updated.rejection_reason is None
    notifier.notify_plan_refined.assert_not_awaited()


@pytest.mark.asyncio
async def test_curate_keep_leaves_plan_unmutated(
    mem_fs: dict[str, str],
) -> None:
    """A ``keep`` verdict returns False and leaves the plan's *content* and
    ``pending_review`` status untouched — but stamps the round-robin cursor
    (``last_curated_at``) so the plan yields its slot next cadence (#2885)."""
    e, m, notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m)
    with patch(
        "maestro.evolution.query",
        AsyncMock(return_value=MockQueryResult(response=json.dumps({"action": "keep"}))),
    ):
        result = await e.curate_pending_plans()

    assert result is False
    updated = m.get_plan("cp1")
    assert updated is not None
    assert updated.status == "pending_review"
    assert updated.refined_at is None
    # The cursor advanced even though the plan body is unchanged.
    assert updated.last_curated_at is not None
    notifier.notify_plan_refined.assert_not_awaited()


@pytest.mark.asyncio
async def test_curate_skip_serviced_flag_threaded_from_config(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 6: curate_pending_plans threads
    loop_curate_skip_serviced_enabled into select_curation_target. With the
    flag on and every pending plan already serviced (kept), the selector
    returns None so the engine early-returns False with no SDK call."""
    d = Path("/fake/curate")
    m = Memory(d)
    c = Consciousness(d)
    c.load_self_model()
    j = Journal(d)
    notifier = MagicMock()
    notifier.notify = AsyncMock()
    issues = IssueTracker(github_repo=None)
    cfg = Config(loop_curate_skip_serviced_enabled=True)
    e = EvolutionEngine(m, c, j, notifier, issues=issues, config=cfg)
    _seed_pending_plan(m, "kept")
    m.touch_plan_curated("kept")

    with patch(
        "maestro.evolution.query",
        AsyncMock(side_effect=AssertionError("no SDK call expected")),
    ):
        result = await e.curate_pending_plans()

    assert result is False


@pytest.mark.asyncio
async def test_curate_keep_advances_selector_to_next_plan(
    mem_fs: dict[str, str],
) -> None:
    """Regression for #2885: a ``keep`` verdict against the head-of-queue plan
    must not re-pick that same plan forever. After one ``keep`` against the
    oldest unrefined plan A, ``select_curation_target`` returns the next plan
    B, not A — the round-robin cursor (``last_curated_at``) moved A behind B."""
    from maestro.audit_checks import select_curation_target

    e, m, _notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m, "A", created_at="2026-05-01T00:00:00+00:00")
    _seed_pending_plan(m, "B", created_at="2026-05-02T00:00:00+00:00")

    # Before any curation, A (oldest, never curated) is the target.
    first = select_curation_target(m.get_all_plans())
    assert first is not None
    assert first.plan_id == "A"

    with patch(
        "maestro.evolution.query",
        AsyncMock(return_value=MockQueryResult(response=json.dumps({"action": "keep"}))),
    ):
        result = await e.curate_pending_plans()

    assert result is False
    # On today's tree without the fix this re-selects A forever.
    second = select_curation_target(m.get_all_plans())
    assert second is not None
    assert second.plan_id == "B"


@pytest.mark.asyncio
async def test_curate_malformed_json_skips(
    mem_fs: dict[str, str],
) -> None:
    """A non-JSON response is logged and skipped with no mutation."""
    e, m, notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m)
    with patch(
        "maestro.evolution.query",
        AsyncMock(return_value=MockQueryResult(response="this is not json")),
    ):
        result = await e.curate_pending_plans()

    assert result is False
    assert m.get_plan("cp1").refined_at is None  # type: ignore[union-attr]
    notifier.notify_plan_refined.assert_not_awaited()


@pytest.mark.asyncio
async def test_curate_non_object_json_skips(
    mem_fs: dict[str, str],
) -> None:
    """A JSON array (not an object) is skipped."""
    e, m, notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m)
    with patch(
        "maestro.evolution.query",
        AsyncMock(return_value=MockQueryResult(response="[]")),
    ):
        result = await e.curate_pending_plans()

    assert result is False
    assert m.get_plan("cp1").status == "pending_review"  # type: ignore[union-attr]
    notifier.notify_plan_refined.assert_not_awaited()


@pytest.mark.asyncio
async def test_curate_unrecognised_action_skips(
    mem_fs: dict[str, str],
) -> None:
    """An unknown ``action`` is skipped without mutation."""
    e, m, notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m)
    with patch(
        "maestro.evolution.query",
        AsyncMock(
            return_value=MockQueryResult(response=json.dumps({"action": "frobnicate"})),
        ),
    ):
        result = await e.curate_pending_plans()

    assert result is False
    assert m.get_plan("cp1").status == "pending_review"  # type: ignore[union-attr]
    notifier.notify_plan_refined.assert_not_awaited()


@pytest.mark.asyncio
async def test_curate_refine_with_no_writable_fields_skips(
    mem_fs: dict[str, str],
) -> None:
    """A ``refine`` verdict carrying none of the three writable fields is a no-op."""
    e, m, notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m)
    with patch(
        "maestro.evolution.query",
        AsyncMock(
            return_value=MockQueryResult(response=json.dumps({"action": "refine"})),
        ),
    ):
        result = await e.curate_pending_plans()

    assert result is False
    assert m.get_plan("cp1").refined_at is None  # type: ignore[union-attr]
    notifier.notify_plan_refined.assert_not_awaited()


@pytest.mark.asyncio
async def test_curate_refine_null_field_treated_as_omitted(
    mem_fs: dict[str, str],
) -> None:
    """A refine verdict that returns one field as JSON ``null`` applies the
    other (valid) field and does not crash — null counts as omitted, not as a
    value forwarded to ``refine_plan`` (which would raise on the wrong type)."""
    e, m, notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m)
    verdict = json.dumps(
        {
            "action": "refine",
            "rationale": "A sharper rationale for the human reviewer.",
            "proposed_changes": None,
        },
    )
    with patch.object(
        e._memory, "refine_plan", wraps=e._memory.refine_plan,
    ) as spy, patch(
        "maestro.evolution.query",
        AsyncMock(return_value=MockQueryResult(response=verdict)),
    ):
        result = await e.curate_pending_plans()

    assert result is True
    # The null field is dropped — only the valid field reaches refine_plan.
    assert spy.call_args.args == ("cp1",)
    assert spy.call_args.kwargs == {
        "rationale": "A sharper rationale for the human reviewer.",
    }
    updated = m.get_plan("cp1")
    assert updated is not None
    assert updated.rationale == "A sharper rationale for the human reviewer."
    # The null field left the original value untouched.
    assert updated.proposed_changes == ["Hash fragments", "Reuse on hit"]
    notifier.notify_plan_refined.assert_awaited_once()


@pytest.mark.asyncio
async def test_curate_refine_rewrites_files_affected(
    mem_fs: dict[str, str],
) -> None:
    """A verdict carrying only ``files_affected`` refines the plan and
    restamps the notified hash (#4520)."""
    e, m, notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m)
    verdict = json.dumps(
        {"action": "refine", "files_affected": ["maestro/memory.py"]},
    )
    with patch(
        "maestro.evolution.query",
        AsyncMock(return_value=MockQueryResult(response=verdict)),
    ):
        result = await e.curate_pending_plans()

    assert result is True
    updated = m.get_plan("cp1")
    assert updated is not None
    assert updated.files_affected == ["maestro/memory.py"]
    assert updated.notified_content_hash == updated.plan_content_hash
    notifier.notify_plan_refined.assert_awaited_once()


@pytest.mark.asyncio
async def test_curate_refine_null_files_affected_left_unchanged(
    mem_fs: dict[str, str],
) -> None:
    e, m, _notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m)
    verdict = json.dumps(
        {"action": "refine", "rationale": "Sharper.", "files_affected": None},
    )
    with patch(
        "maestro.evolution.query",
        AsyncMock(return_value=MockQueryResult(response=verdict)),
    ):
        result = await e.curate_pending_plans()

    assert result is True
    updated = m.get_plan("cp1")
    assert updated is not None
    assert updated.rationale == "Sharper."
    assert updated.files_affected == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "files",
    [
        ["x" * (_PLAN_FILE_PATH_MAX_CHARS + 1)],
        ["maestro/memory.py", "   "],
        ["f.py"] * (_PLAN_FILES_AFFECTED_MAX_ITEMS + 1),
    ],
)
async def test_curate_refine_bad_files_affected_skips(
    mem_fs: dict[str, str],
    caplog: pytest.LogCaptureFixture,
    files: list[str],
) -> None:
    """An over-long, blank or over-count ``files_affected`` is skipped with a
    WARNING and a cursor advance, not a crash (#3339 handling)."""
    e, m, notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m)
    verdict = json.dumps({"action": "refine", "files_affected": files})
    with patch(
        "maestro.evolution.query",
        AsyncMock(return_value=MockQueryResult(response=verdict)),
    ), caplog.at_level(logging.WARNING, logger="maestro.evolution"):
        result = await e.curate_pending_plans()

    assert result is False
    stored = m.get_plan("cp1")
    assert stored is not None
    assert stored.files_affected == []
    assert stored.refined_at is None
    assert stored.last_curated_at is not None
    assert "files_affected" in caplog.text
    notifier.notify_plan_refined.assert_not_awaited()


@pytest.mark.asyncio
async def test_curate_refine_all_null_fields_skips(
    mem_fs: dict[str, str],
) -> None:
    """A refine verdict whose only writable fields are ``null`` is a no-op —
    after dropping nulls nothing remains to write."""
    e, m, notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m)
    verdict = json.dumps({"action": "refine", "rationale": None})
    with patch(
        "maestro.evolution.query",
        AsyncMock(return_value=MockQueryResult(response=verdict)),
    ):
        result = await e.curate_pending_plans()

    assert result is False
    assert m.get_plan("cp1").refined_at is None  # type: ignore[union-attr]
    notifier.notify_plan_refined.assert_not_awaited()


@pytest.mark.asyncio
async def test_curate_obsolete_missing_reason_skips(
    mem_fs: dict[str, str],
) -> None:
    """An ``obsolete`` verdict with no usable reason is skipped."""
    e, m, _notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m)
    with patch(
        "maestro.evolution.query",
        AsyncMock(
            return_value=MockQueryResult(response=json.dumps({"action": "obsolete"})),
        ),
    ):
        result = await e.curate_pending_plans()

    assert result is False
    assert m.get_plan("cp1").status == "pending_review"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_curate_obsolete_skips_over_length_reason(
    mem_fs: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An ``obsolete`` reason over the 280-char cap is normal model-output
    jitter, not a Maestro invariant violation — it is skipped (no mutation, no
    ValueError escapes), unlike the refine path which surfaces cap violations
    loudly. Guards against the crash-loop in #2699: the write boundary's cap
    raise must never reach the loop for a terminal prune annotation."""
    from maestro.memory import _OBSOLETE_REASON_MAX_CHARS

    e, m, _notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m)
    over_length = "x" * (_OBSOLETE_REASON_MAX_CHARS + 10)
    verdict = json.dumps({"action": "obsolete", "reason": over_length})
    with patch.object(
        e._memory, "mark_plan_obsolete", wraps=e._memory.mark_plan_obsolete,
    ) as spy, patch(
        "maestro.evolution.query",
        AsyncMock(return_value=MockQueryResult(response=verdict)),
    ), caplog.at_level("WARNING", logger="maestro"):
        result = await e.curate_pending_plans()

    assert result is False
    # The guard short-circuits before the write boundary — its cap raise
    # (the crash source) is never reached.
    spy.assert_not_called()
    updated = m.get_plan("cp1")
    assert updated is not None
    assert updated.status == "pending_review"
    assert updated.obsolete_reason is None
    assert any(
        "cp1" in rec.message and str(_OBSOLETE_REASON_MAX_CHARS) in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_curate_refine_skips_over_length_change_item(
    mem_fs: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``proposed_changes`` item over the per-item cap is normal model-output
    jitter, not a Maestro invariant violation — it is skipped (no mutation, no
    ValueError escapes), the direct parallel of
    ``test_curate_obsolete_skips_over_length_reason``. Guards against the
    crash-loop in #3339: the guard short-circuits before the write boundary so
    ``refine_plan``'s cap raise never reaches the loop."""
    from maestro.memory import _PLAN_CHANGE_ITEM_MAX_CHARS

    e, m, notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m)
    over_item = "x" * (_PLAN_CHANGE_ITEM_MAX_CHARS + 10)
    verdict = json.dumps(
        {"action": "refine", "proposed_changes": [over_item, "Reuse on hit"]},
    )
    with patch.object(
        e._memory, "refine_plan", wraps=e._memory.refine_plan,
    ) as spy, patch(
        "maestro.evolution.query",
        AsyncMock(return_value=MockQueryResult(response=verdict)),
    ), caplog.at_level("WARNING", logger="maestro"):
        result = await e.curate_pending_plans()

    assert result is False
    # The guard short-circuits before the write boundary — its cap raise
    # (the crash source) is never reached.
    spy.assert_not_called()
    updated = m.get_plan("cp1")
    assert updated is not None
    assert updated.status == "pending_review"
    assert updated.refined_at is None
    assert any(
        "cp1" in rec.message and str(_PLAN_CHANGE_ITEM_MAX_CHARS) in rec.message
        for rec in caplog.records
    )
    notifier.notify_plan_refined.assert_not_awaited()


@pytest.mark.asyncio
async def test_curate_refine_skips_over_length_acceptance_criterion(
    mem_fs: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#4104: an acceptance criterion over the 200-char criterion cap (well
    under the 1000-char change cap) is skipped with a WARNING naming the
    criterion cap, never reaching ``refine_plan``'s raise (#3339)."""
    from maestro.types import ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS

    e, m, notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m)
    over_item = "x" * (ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS + 1)
    verdict = json.dumps({"action": "refine", "acceptance_criteria": [over_item]})
    with patch.object(
        e._memory, "refine_plan", wraps=e._memory.refine_plan,
    ) as spy, patch(
        "maestro.evolution.query",
        AsyncMock(return_value=MockQueryResult(response=verdict)),
    ), caplog.at_level("WARNING", logger="maestro"):
        result = await e.curate_pending_plans()

    assert result is False
    spy.assert_not_called()
    updated = m.get_plan("cp1")
    assert updated is not None
    assert updated.status == "pending_review"
    assert updated.refined_at is None
    assert updated.last_curated_at is not None
    assert any(
        "cp1" in rec.message
        and str(ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS) in rec.message
        for rec in caplog.records
    )
    notifier.notify_plan_refined.assert_not_awaited()


@pytest.mark.asyncio
async def test_curate_refine_skips_over_count_acceptance_criteria(
    mem_fs: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refine verdict with more than ``ACCEPTANCE_CRITERIA_MAX_ITEMS`` ``acceptance_criteria`` (each within
    the per-item char cap) is normal model-output jitter, not a Maestro
    invariant violation — the caller skips it (no mutation, no ValueError
    escapes) so ``refine_plan``'s new count-cap raise never reaches the loop
    (#3339 crash-loop guard). The free-form curate verdict has no ``maxItems``
    schema, so this caller pre-check is the sole guard on the count cap."""
    from maestro.types import ACCEPTANCE_CRITERIA_MAX_ITEMS

    e, m, notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m)
    over_count = [
        f"Criterion {i} is asserted by a test." for i in range(ACCEPTANCE_CRITERIA_MAX_ITEMS + 1)
    ]
    verdict = json.dumps(
        {"action": "refine", "acceptance_criteria": over_count},
    )
    with patch.object(
        e._memory, "refine_plan", wraps=e._memory.refine_plan,
    ) as spy, patch(
        "maestro.evolution.query",
        AsyncMock(return_value=MockQueryResult(response=verdict)),
    ), caplog.at_level("WARNING", logger="maestro"):
        result = await e.curate_pending_plans()

    assert result is False
    # The guard short-circuits before the write boundary — its count-cap raise
    # (the crash source) is never reached.
    spy.assert_not_called()
    updated = m.get_plan("cp1")
    assert updated is not None
    assert updated.status == "pending_review"
    assert updated.refined_at is None
    # The round-robin cursor is advanced so the skipped plan yields its slot.
    assert updated.last_curated_at is not None
    assert any(
        "cp1" in rec.message and str(ACCEPTANCE_CRITERIA_MAX_ITEMS) in rec.message
        for rec in caplog.records
    )
    notifier.notify_plan_refined.assert_not_awaited()


@pytest.mark.asyncio
async def test_curate_refine_skips_over_length_rationale(
    mem_fs: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An over-cap ``rationale`` (over 2000 chars) is normal model-output
    jitter, not a Maestro invariant violation — it is skipped (no mutation, no
    ValueError escapes), mirroring the ``obsolete`` sibling rather than the
    former fail-loud crash. Guards against the crash-loop in #3339."""
    from maestro.memory import _PLAN_RATIONALE_MAX_CHARS

    e, m, notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m)
    over_length = "x" * (_PLAN_RATIONALE_MAX_CHARS + 1)
    verdict = json.dumps({"action": "refine", "rationale": over_length})
    with patch.object(
        e._memory, "refine_plan", wraps=e._memory.refine_plan,
    ) as spy, patch(
        "maestro.evolution.query",
        AsyncMock(return_value=MockQueryResult(response=verdict)),
    ), caplog.at_level("WARNING", logger="maestro"):
        result = await e.curate_pending_plans()

    assert result is False
    spy.assert_not_called()
    updated = m.get_plan("cp1")
    assert updated is not None
    assert updated.status == "pending_review"
    assert updated.refined_at is None
    assert any(
        "cp1" in rec.message and str(_PLAN_RATIONALE_MAX_CHARS) in rec.message
        for rec in caplog.records
    )
    notifier.notify_plan_refined.assert_not_awaited()


@pytest.mark.asyncio
async def test_curate_refine_empty_string_field_raises(
    mem_fs: dict[str, str],
) -> None:
    """An empty/whitespace rewritten field is non-``null``, so it passes the
    ``is not None`` filter into refine_plan, whose non-empty validator rejects
    it — the ValueError propagates (fail loud) and the plan is unmutated. This
    pins the deliberate null-vs-empty asymmetry: ``null`` is dropped as
    omitted, but ``""`` is a contract violation that surfaces loudly."""
    e, m, notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m)
    verdict = json.dumps({"action": "refine", "rationale": "   "})
    with patch(
        "maestro.evolution.query",
        AsyncMock(return_value=MockQueryResult(response=verdict)),
    ), pytest.raises(ValueError, match="rationale must be non-empty"):
        await e.curate_pending_plans()

    assert m.get_plan("cp1").refined_at is None  # type: ignore[union-attr]
    notifier.notify_plan_refined.assert_not_awaited()


@pytest.mark.asyncio
async def test_curate_refine_wrong_typed_list_item_raises(
    mem_fs: dict[str, str],
) -> None:
    """A ``proposed_changes`` list carrying a non-string item is a broken
    contract, not model-output jitter. The over-cap guard fires only on
    correctly-typed lists (its ``all(isinstance str)`` clause is False here),
    so the wrong-typed item falls straight through to ``refine_plan``'s
    ``list[str]`` validator and surfaces loudly (fail loud, plan unmutated).
    Pins the #3339 design intent: over-cap fields skip gracefully while
    wrong-typed ones stay fail-loud."""
    e, m, notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m)
    verdict = json.dumps({"action": "refine", "proposed_changes": ["ok", 123]})
    with patch(
        "maestro.evolution.query",
        AsyncMock(return_value=MockQueryResult(response=verdict)),
    ), pytest.raises(TypeError, match=r"proposed_changes must be list\[str\]"):
        await e.curate_pending_plans()

    assert m.get_plan("cp1").refined_at is None  # type: ignore[union-attr]
    notifier.notify_plan_refined.assert_not_awaited()


@pytest.mark.asyncio
async def test_curate_refine_plan_no_longer_pending_skips_notify(
    mem_fs: dict[str, str],
) -> None:
    """If the plan left pending_review between selection and write,
    refine_plan returns False and no notification fires."""
    e, m, notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m)
    verdict = json.dumps(
        {"action": "refine", "rationale": "A clearer rationale for review."},
    )
    with patch.object(e._memory, "refine_plan", return_value=False), patch(
        "maestro.evolution.query",
        AsyncMock(return_value=MockQueryResult(response=verdict)),
    ):
        result = await e.curate_pending_plans()

    assert result is False
    notifier.notify_plan_refined.assert_not_awaited()


@pytest.mark.asyncio
async def test_curate_obsolete_no_longer_pending_returns_false(
    mem_fs: dict[str, str],
) -> None:
    """mark_plan_obsolete returning False (race) yields a no-op result."""
    e, m, _notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m)
    verdict = json.dumps({"action": "obsolete", "reason": "Stale."})
    with patch.object(e._memory, "mark_plan_obsolete", return_value=False), patch(
        "maestro.evolution.query",
        AsyncMock(return_value=MockQueryResult(response=verdict)),
    ):
        result = await e.curate_pending_plans()

    assert result is False


@pytest.mark.asyncio
async def test_curate_prompt_renders_none_for_empty_fields(
    mem_fs: dict[str, str],
) -> None:
    """Empty rationale / changes / criteria render as ``(none)`` in the prompt,
    and the target plan's title is injected."""
    e, m, _notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(
        m, rationale="", proposed_changes=[], acceptance_criteria=[],
    )
    with patch(
        "maestro.evolution.query",
        AsyncMock(return_value=MockQueryResult(response=json.dumps({"action": "keep"}))),
    ) as mock_q:
        await e.curate_pending_plans()

    prompt = mock_q.call_args.kwargs.get("prompt", "")
    assert "(none)" in prompt
    assert "Cache prompt fragments" in prompt


@pytest.mark.asyncio
async def test_curate_refine_notification_failure_is_swallowed(
    mem_fs: dict[str, str],
) -> None:
    """A notifier outage after a successful refine must not undo the refine
    nor crash the phase (best-effort, mirroring submit_for_review)."""
    e, m, notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m)
    notifier.notify_plan_refined = AsyncMock(side_effect=RuntimeError("telegram down"))
    verdict = json.dumps(
        {"action": "refine", "rationale": "A clearer rationale for the reviewer."},
    )
    with patch(
        "maestro.evolution.query",
        AsyncMock(return_value=MockQueryResult(response=verdict)),
    ):
        result = await e.curate_pending_plans()

    assert result is True
    assert m.get_plan("cp1").refined_at is not None  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Proposal 155 step 6 — the curator stamps the curation fingerprint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_curate_keep_stamps_fingerprint_and_skips_next(
    mem_fs: dict[str, str],
) -> None:
    from maestro.memory import compute_curation_fingerprint

    e, m, _notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m)
    with patch(
        "maestro.evolution.query",
        AsyncMock(return_value=MockQueryResult(response=json.dumps({"action": "keep"}))),
    ):
        assert await e.curate_pending_plans() is False
    plan = m.get_plan("cp1")
    assert plan is not None
    assert plan.curated_hash == compute_curation_fingerprint(plan)
    with patch(
        "maestro.evolution.query",
        AsyncMock(side_effect=AssertionError("no SDK call expected")),
    ):
        assert await e.curate_pending_plans() is False


@pytest.mark.asyncio
async def test_curate_refine_stamps_post_refine_fingerprint(
    mem_fs: dict[str, str],
) -> None:
    from maestro.memory import compute_curation_fingerprint

    e, m, _notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m)
    verdict = json.dumps({"action": "refine", "rationale": "Sharper rationale."})
    with patch(
        "maestro.evolution.query",
        AsyncMock(return_value=MockQueryResult(response=verdict)),
    ):
        assert await e.curate_pending_plans() is True
    plan = m.get_plan("cp1")
    assert plan is not None
    assert plan.rationale == "Sharper rationale."
    assert plan.curated_hash == compute_curation_fingerprint(plan)
    mock_q = AsyncMock()
    with patch("maestro.evolution.query", mock_q):
        assert await e.curate_pending_plans() is False
    mock_q.assert_not_called()


@pytest.mark.asyncio
async def test_curate_malformed_json_stamps_fingerprint(
    mem_fs: dict[str, str],
) -> None:
    from maestro.memory import compute_curation_fingerprint

    e, m, _notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m)
    with patch(
        "maestro.evolution.query",
        AsyncMock(return_value=MockQueryResult(response="this is not json")),
    ):
        assert await e.curate_pending_plans() is False
    plan = m.get_plan("cp1")
    assert plan is not None
    assert plan.curated_hash == compute_curation_fingerprint(plan)
