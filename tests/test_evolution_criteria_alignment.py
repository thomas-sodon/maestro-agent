"""Acceptance-criteria alignment; split out of ``tests/test_evolution.py``.
"""

from __future__ import annotations

import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro import evolution_quality
from maestro.config import Config
from maestro.memory import (
    EvolutionPlan,
)
from tests.conftest import MockQueryResult, arm_entry_clean_tree
from tests.test_evolution import (
    _FAIL_REVIEW,
    _make,
)

# ------------------------------------------------------------------
# Acceptance-criteria alignment (criteria_alignment_enabled)
# ------------------------------------------------------------------

_AC_CRITERIA = ["AcCrit alpha holds true.", "AcCrit beta holds true."]


def _ac_plan(
    criteria: list[str], *, source_task_id: str | None = None,
) -> EvolutionPlan:
    return EvolutionPlan(
        plan_id="pAC", title="t",
        category="capability", rationale="r", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="t",
        status="approved", created_at="2025-01-01T00:00:00Z",
        acceptance_criteria=list(criteria),
        source_task_id=source_task_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("flag_on", [True, False])
@pytest.mark.parametrize("criteria", [_AC_CRITERIA, []])
async def test_implement_injects_criteria_under_flag(
    mem_fs: dict[str, str], flag_on: bool, criteria: list[str],
) -> None:
    """implement() shows each criterion only when flag-on and criteria present."""
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(criteria_alignment_enabled=flag_on)
    plan = _ac_plan(criteria)

    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.get_head_hash.return_value = "abc123"
    arm_entry_clean_tree(vcs)
    e._vcs = vcs

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="done")
        await e.implement(plan)

    prompt = mock_q.call_args.kwargs["prompt"]
    should_appear = flag_on and bool(criteria)
    for c in _AC_CRITERIA:
        if should_appear:
            assert c in prompt
        else:
            assert c not in prompt


def _resume_plan() -> EvolutionPlan:
    """A ``failed`` plan carrying proposal-131 resume context — a prior
    strictly-progressing stall preserved this branch at ``resume_branch_head``."""
    return EvolutionPlan(
        plan_id="pRESUME", title="t",
        category="capability", rationale="r", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="t",
        status="failed", created_at="2025-01-01T00:00:00Z",
        failure_count=1,
        resume_count=1,
        resume_failing_identities=["tests:A", "tests:B"],
        resume_branch_head="RESUMEHEAD" + "0" * 30,
    )


def _blocked_answer_plan() -> EvolutionPlan:
    """An ``approved`` plan re-admitted after Thomas answered its blocked
    question — carrying the human's answer text for the next attempt. No resume
    context (the ASK_HUMAN arm disposed of the branch), so implement() rebuilds
    fresh with only the answer preamble as carried context."""
    return EvolutionPlan(
        plan_id="pREADMIT", title="t",
        category="capability", rationale="r", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="t",
        status="approved", created_at="2025-01-01T00:00:00Z",
        failure_count=2,
        blocked_answer="the CI runner is fixed, use the fallback queue",
    )


@pytest.mark.asyncio
async def test_implement_resume_path_continues_from_preserved_branch(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 131: with the flag on and valid resume context, implement()
    continues from the preserved branch — it checks the branch out, merges
    origin/main, skips the destructive recreate, and prepends the resume preamble
    naming the prior failing identities."""
    from maestro.vcs import evolution_branch

    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(
        attempt_disposition_enabled=True, criteria_alignment_enabled=False,
    )
    plan = _resume_plan()
    branch = evolution_branch(plan.plan_id)

    vcs = MagicMock()
    vcs.rev_parse.return_value = plan.resume_branch_head
    vcs.merge_from.return_value = MagicMock(success=True)
    vcs.get_head_hash.return_value = "abc123"
    arm_entry_clean_tree(vcs)
    e._vcs = vcs

    with (
        patch.object(e, "_observe_baseline", MagicMock(return_value=None)),
        patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q,
    ):
        mock_q.return_value = MockQueryResult(response="done")
        await e.implement(plan)

    # Resume path: existing branch checked out + origin/main merged, and the
    # destructive branch recreate is skipped entirely.
    vcs.checkout.assert_any_call(branch)
    vcs.merge_from.assert_called_once_with(
        "origin/main",
        message="maestro: merge main into evolution branch before resume",
    )
    vcs.checkout_new_branch.assert_not_called()

    prompt = mock_q.call_args.kwargs["prompt"]
    assert "RESUMING" in prompt
    assert "tests:A" in prompt
    assert "tests:B" in prompt


@pytest.mark.asyncio
async def test_implement_resume_inert_with_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Flag-off: resume context is ignored — implement() cold-restarts (creates a
    fresh evolution branch), no resume merge fires, and no resume preamble reaches
    the prompt (byte-identical to today)."""
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(
        attempt_disposition_enabled=False, criteria_alignment_enabled=False,
    )
    plan = _resume_plan()

    vcs = MagicMock()
    vcs.rev_parse.return_value = plan.resume_branch_head
    vcs.get_current_branch.return_value = "main"
    vcs.get_head_hash.return_value = "abc123"
    arm_entry_clean_tree(vcs)
    e._vcs = vcs

    with (
        patch.object(e, "_observe_baseline", MagicMock(return_value=None)),
        patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q,
    ):
        mock_q.return_value = MockQueryResult(response="done")
        await e.implement(plan)

    # Cold-start: the fresh evolution branch is created and no resume merge fires.
    vcs.checkout_new_branch.assert_called_once()
    vcs.merge_from.assert_not_called()
    prompt = mock_q.call_args.kwargs["prompt"]
    assert "RESUMING" not in prompt


@pytest.mark.asyncio
async def test_implement_blocked_answer_prepended_with_flag_on(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 131 re-admission: with the flag on and blocked_answer set, the
    implementation prompt carries the human's answer text. The plan rebuilds fresh
    (no resume context — the ASK_HUMAN arm disposed of the branch), so the answer
    preamble is the only carried context."""
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(
        attempt_disposition_enabled=True, criteria_alignment_enabled=False,
    )
    plan = _blocked_answer_plan()

    vcs = MagicMock()
    vcs.rev_parse.return_value = ""
    vcs.get_head_hash.return_value = "abc123"
    arm_entry_clean_tree(vcs)
    e._vcs = vcs

    with (
        patch.object(e, "_observe_baseline", MagicMock(return_value=None)),
        patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q,
    ):
        mock_q.return_value = MockQueryResult(response="done")
        await e.implement(plan)

    # Fresh cold-start (no resume merge), with the answer carried into the prompt.
    vcs.checkout_new_branch.assert_called_once()
    vcs.merge_from.assert_not_called()
    prompt = mock_q.call_args.kwargs["prompt"]
    assert "the CI runner is fixed, use the fallback queue" in prompt
    assert "RESUMING" not in prompt


@pytest.mark.asyncio
async def test_implement_blocked_answer_inert_with_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Flag-off: a blocked_answer is ignored — the prompt does not carry it
    (byte-identical to today)."""
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(
        attempt_disposition_enabled=False, criteria_alignment_enabled=False,
    )
    plan = _blocked_answer_plan()

    vcs = MagicMock()
    vcs.rev_parse.return_value = ""
    vcs.get_head_hash.return_value = "abc123"
    arm_entry_clean_tree(vcs)
    e._vcs = vcs

    with (
        patch.object(e, "_observe_baseline", MagicMock(return_value=None)),
        patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q,
    ):
        mock_q.return_value = MockQueryResult(response="done")
        await e.implement(plan)

    prompt = mock_q.call_args.kwargs["prompt"]
    assert "the CI runner is fixed, use the fallback queue" not in prompt


@pytest.mark.asyncio
async def test_implement_no_blocked_answer_no_preamble_with_flag_on(
    mem_fs: dict[str, str],
) -> None:
    """Flag on but no blocked_answer (an ordinary approved plan): the re-admission
    preamble does not reach the prompt."""
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(
        attempt_disposition_enabled=True, criteria_alignment_enabled=False,
    )
    plan = _blocked_answer_plan()
    plan.blocked_answer = None

    vcs = MagicMock()
    vcs.rev_parse.return_value = ""
    vcs.get_head_hash.return_value = "abc123"
    arm_entry_clean_tree(vcs)
    e._vcs = vcs

    with (
        patch.object(e, "_observe_baseline", MagicMock(return_value=None)),
        patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q,
    ):
        mock_q.return_value = MockQueryResult(response="done")
        await e.implement(plan)

    prompt = mock_q.call_args.kwargs["prompt"]
    assert "This plan was previously PAUSED" not in prompt


@pytest.mark.asyncio
async def test_implement_blocked_answer_cleared_after_consumption(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 131 (r4): blocked_answer is one-shot. Once implement() injects it
    into the prompt it is cleared in memory (and on the in-hand snapshot), so a
    later retry of the same plan does not re-prepend Thomas's original decision."""
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(
        attempt_disposition_enabled=True, criteria_alignment_enabled=False,
    )
    plan = _blocked_answer_plan()
    _m.save_evolution_plan(plan)

    vcs = MagicMock()
    vcs.rev_parse.return_value = ""
    vcs.get_head_hash.return_value = "abc123"
    arm_entry_clean_tree(vcs)
    e._vcs = vcs

    with (
        patch.object(e, "_observe_baseline", MagicMock(return_value=None)),
        patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q,
    ):
        mock_q.return_value = MockQueryResult(response="done")
        await e.implement(plan)

    # The answer reached this attempt's prompt...
    prompt = mock_q.call_args.kwargs["prompt"]
    assert "the CI runner is fixed, use the fallback queue" in prompt
    # ...but is consumed: cleared on the persisted plan and the in-hand snapshot.
    reloaded = _m.get_plan("pREADMIT")
    assert reloaded is not None
    assert reloaded.blocked_answer is None
    assert plan.blocked_answer is None


@pytest.mark.asyncio
async def test_implement_resume_merge_conflict_falls_back_to_fresh(
    mem_fs: dict[str, str],
) -> None:
    """A resume whose origin/main merge conflicts aborts the merge, returns to the
    original branch, and falls back to the fresh-branch build (no resume
    preamble) — mirroring the source_task_id conflict fallback."""
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(
        attempt_disposition_enabled=True, criteria_alignment_enabled=False,
    )
    plan = _resume_plan()

    vcs = MagicMock()
    vcs.rev_parse.return_value = plan.resume_branch_head
    vcs.merge_from.return_value = MagicMock(success=False)
    vcs.get_current_branch.return_value = "main"
    vcs.get_head_hash.return_value = "abc123"
    arm_entry_clean_tree(vcs)
    e._vcs = vcs

    with (
        patch.object(e, "_observe_baseline", MagicMock(return_value=None)),
        patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q,
    ):
        mock_q.return_value = MockQueryResult(response="done")
        await e.implement(plan)

    vcs.merge_from.assert_called_once()
    vcs.abort_merge.assert_called_once()
    vcs.checkout_new_branch.assert_called_once()  # fresh-branch fallback
    prompt = mock_q.call_args.kwargs["prompt"]
    assert "RESUMING" not in prompt


@pytest.mark.asyncio
async def test_implement_resume_vcs_error_falls_back_to_fresh(
    mem_fs: dict[str, str],
) -> None:
    """A VCSError during the resume rebase is caught (merge aborted, original
    branch restored) and the fresh-branch build takes over rather than the error
    propagating."""
    from maestro.vcs import VCSError

    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(
        attempt_disposition_enabled=True, criteria_alignment_enabled=False,
    )
    plan = _resume_plan()

    vcs = MagicMock()
    vcs.rev_parse.return_value = plan.resume_branch_head
    vcs.merge_from.side_effect = VCSError("boom")
    vcs.get_current_branch.return_value = "main"
    vcs.get_head_hash.return_value = "abc123"
    arm_entry_clean_tree(vcs)
    e._vcs = vcs

    with (
        patch.object(e, "_observe_baseline", MagicMock(return_value=None)),
        patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q,
    ):
        mock_q.return_value = MockQueryResult(response="done")
        await e.implement(plan)

    vcs.abort_merge.assert_called_once()
    vcs.checkout_new_branch.assert_called_once()
    prompt = mock_q.call_args.kwargs["prompt"]
    assert "RESUMING" not in prompt


@pytest.mark.asyncio
async def test_implement_resume_no_new_changes_keeps_branch(
    mem_fs: dict[str, str],
) -> None:
    """A resume that produces no *new* changes keeps the accumulated branch work
    (like the source-task path) rather than raising 'no file changes'. The base
    returned is origin/main — the branch's true fork point after the merge — not
    the preserved branch tip (proposal 131 / ledger r1)."""
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(
        attempt_disposition_enabled=True, criteria_alignment_enabled=False,
    )
    plan = _resume_plan()
    origin_main = "deadbeef" + "0" * 32

    vcs = MagicMock()
    vcs.rev_parse.side_effect = lambda ref: (
        origin_main if ref == "origin/main" else plan.resume_branch_head
    )
    vcs.merge_from.return_value = MagicMock(success=True)
    vcs.get_head_hash.return_value = "abc123"
    arm_entry_clean_tree(vcs, later=False)  # no new commits this attempt
    e._vcs = vcs

    with (
        patch.object(e, "_observe_baseline", MagicMock(return_value=None)),
        patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q,
    ):
        mock_q.return_value = MockQueryResult(response="done")
        base = await e.implement(plan)  # must not raise

    # Resume base is origin/main (spans the whole plan diff), never the tip.
    assert base == origin_main
    vcs.commit.assert_not_called()


@pytest.mark.asyncio
async def test_implement_resume_bases_attribution_on_origin_main_not_tip(
    mem_fs: dict[str, str],
) -> None:
    """Ledger r1 regression: on the resume path the base returned for self-review
    and attribution is origin/main — the branch's true fork point after the merge
    — not the preserved branch tip, and the tip's (red) gate state is never banked
    as the attribution baseline.

    Basing on the tip would let attribute() misread the prior attempt's own
    still-failing identities (a subset of the tip's red) as INHERITED, routing a
    genuinely-progressing resume into the destructive INHERITED hold instead of
    continuing. Verifies both halves of the fix: the returned base is origin/main,
    and _observe_baseline is never run against the polluting tip on this path."""
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(
        attempt_disposition_enabled=True, criteria_alignment_enabled=False,
    )
    plan = _resume_plan()
    origin_main = "cafef00d" + "0" * 32

    vcs = MagicMock()
    vcs.rev_parse.side_effect = lambda ref: (
        origin_main if ref == "origin/main" else plan.resume_branch_head
    )
    vcs.merge_from.return_value = MagicMock(success=True)
    vcs.get_head_hash.return_value = plan.resume_branch_head  # tip == HEAD
    arm_entry_clean_tree(vcs)
    e._vcs = vcs

    observe_mock = MagicMock(return_value=None)
    with (
        patch.object(e, "_observe_baseline", observe_mock),
        patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q,
    ):
        mock_q.return_value = MockQueryResult(response="done")
        base = await e.implement(plan)

    # Base for self-review + attribution is origin/main, not the tip.
    assert base == origin_main
    assert base != plan.resume_branch_head
    # The polluted branch tip is never banked as an attribution baseline.
    observe_mock.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("flag_on", [True, False])
@pytest.mark.parametrize("criteria", [_AC_CRITERIA, []])
async def test_auto_fix_injects_criteria_under_flag(
    mem_fs: dict[str, str], flag_on: bool, criteria: list[str],
) -> None:
    """auto_fix() shows each criterion only when flag-on and criteria present."""
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(criteria_alignment_enabled=flag_on)
    plan = _ac_plan(criteria)

    vcs = MagicMock()
    vcs.has_changes.return_value = True
    e._vcs = vcs

    with patch("maestro.evolution.subprocess.run") as mock_sub, \
         patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q:
        mock_sub.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="diff", stderr="",
        )
        mock_q.return_value = MockQueryResult(response="fixed")
        await e.auto_fix(plan, _FAIL_REVIEW, round_n=1, max_rounds=3)

    prompt = mock_q.call_args.kwargs["prompt"]
    should_appear = flag_on and bool(criteria)
    for c in _AC_CRITERIA:
        if should_appear:
            assert c in prompt
        else:
            assert c not in prompt


@pytest.mark.asyncio
async def test_criteria_alignment_spans_writing_and_review_phases(
    mem_fs: dict[str, str],
) -> None:
    """implement(), auto_fix(), and self_review() each carry every criterion."""
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(
        criteria_alignment_enabled=True, self_review_max_rounds=1,
    )
    plan = _ac_plan(_AC_CRITERIA)

    # implement()
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.get_head_hash.return_value = "abc123"
    arm_entry_clean_tree(vcs)
    e._vcs = vcs
    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="done")
        await e.implement(plan)
    implement_prompt = mock_q.call_args.kwargs["prompt"]

    # auto_fix()
    vcs_af = MagicMock()
    arm_entry_clean_tree(vcs_af)
    e._vcs = vcs_af
    with patch("maestro.evolution.subprocess.run") as mock_sub, \
         patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q_af:
        mock_sub.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="diff", stderr="",
        )
        mock_q_af.return_value = MockQueryResult(response="fixed")
        await e.auto_fix(plan, _FAIL_REVIEW, round_n=1, max_rounds=3)
    auto_fix_prompt = mock_q_af.call_args.kwargs["prompt"]

    # self_review()
    vcs_sr = MagicMock()
    vcs_sr.get_head_hash.return_value = "deadbeef"
    e._vcs = vcs_sr
    with patch("maestro.evolution.subprocess.run") as mock_sub_sr, \
         patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q_sr:
        mock_sub_sr.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="diff text", stderr="",
        )
        mock_q_sr.return_value = MockQueryResult(response="LGTM")
        await e.self_review(plan, base_hash="base")
    self_review_prompt = mock_q_sr.call_args.kwargs["prompt"]

    for c in _AC_CRITERIA:
        assert c in implement_prompt
        assert c in auto_fix_prompt
        assert c in self_review_prompt


@pytest.mark.asyncio
async def test_criteria_alignment_guard_catches_dropped_criterion(
    mem_fs: dict[str, str],
) -> None:
    """A criterion absent from the plan is absent from the writer prompt.

    Proves the cross-phase ``assert c in prompt`` guard has teeth: drop the
    second criterion and the guard must fail for it.
    """
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(criteria_alignment_enabled=True)
    plan = _ac_plan(_AC_CRITERIA[:1])  # second criterion dropped

    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.get_head_hash.return_value = "abc123"
    arm_entry_clean_tree(vcs)
    e._vcs = vcs

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="done")
        await e.implement(plan)

    prompt = mock_q.call_args.kwargs["prompt"]
    assert _AC_CRITERIA[0] in prompt
    assert _AC_CRITERIA[1] not in prompt
    with pytest.raises(AssertionError):
        assert _AC_CRITERIA[1] in prompt


@pytest.mark.asyncio
async def test_implement_appends_reconciliation_note_on_source_task(
    mem_fs: dict[str, str],
) -> None:
    """The reconciliation note appears when implement() rebases onto a
    source-task branch (base_ref != 'main')."""
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(criteria_alignment_enabled=True)
    plan = _ac_plan(_AC_CRITERIA, source_task_id="t1")

    e._memory = MagicMock()
    e._memory.get_task_record.return_value = MagicMock(branch="maestro/task/t1")

    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.get_head_hash.return_value = "abc123"
    arm_entry_clean_tree(vcs)
    vcs.merge_from.return_value = MagicMock(success=True)
    e._vcs = vcs

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="done")
        await e.implement(plan)

    prompt = mock_q.call_args.kwargs["prompt"]
    assert "predate these criteria and must be reconciled" in prompt


@pytest.mark.asyncio
async def test_implement_omits_reconciliation_note_on_fresh_main(
    mem_fs: dict[str, str],
) -> None:
    """The reconciliation note is absent on the fresh-from-main path, but the
    criteria block itself is still present."""
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(criteria_alignment_enabled=True)
    plan = _ac_plan(_AC_CRITERIA)  # no source_task_id

    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.get_head_hash.return_value = "abc123"
    arm_entry_clean_tree(vcs)
    e._vcs = vcs

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="done")
        await e.implement(plan)

    prompt = mock_q.call_args.kwargs["prompt"]
    assert "predate these criteria" not in prompt
    for c in _AC_CRITERIA:
        assert c in prompt
