"""Collaborator-direct tests for ``maestro.loop.pr_remediation`` (068 step 1).

These construct :class:`PrRemediationController` against a narrow stub
agent instead of a full ``MaestroAgent``, proving the extracted cluster
is separately testable. Behavioural equivalence with the pre-extraction
code is carried by the unchanged ``tests/test_agent*.py`` /
``tests/test_pr_*.py`` suites, which drive the same bodies through the
delegating shims — this file covers the collaborator's own surface:
construction, gate short-circuits, and the ``dequeue_gates`` ordering
contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.consciousness import PendingPRRequest
from maestro.escalation_bridge import JsonEscalationLedger, has_standdown
from maestro.evolution_tend import CI_FAILURE_ON_HEAD_SUMMARY, TendPRsResult
from maestro.loop.pr_remediation import (
    _DEQUEUE_GATE_TABLE,
    _REPLY_COMPLETION_PROBLEM_MARKER,
    _REPLY_MANIFEST_FILENAME,
    PrRemediationController,
    RedRootDiagnosisError,
    _DequeueContext,
    _format_bug_comments_problem,
    _format_conflict_problem,
    _format_quality_gates_problem,
    _format_review_feedback_problem,
    _tend_clearable_blockers,
    read_thread_reply_manifest,
)
from maestro.memory import EvolutionPlan
from maestro.pr_facts import MergeTruth
from maestro.pr_remediation_state import PrRemediationState
from maestro.remediation_tracker import (
    MERGE_BLOCK_AWAITING_PUSH,
    MERGE_BLOCK_BEHIND_MAIN,
    MERGE_BLOCK_CI_FAILING,
    JsonlStandDownVerdictLedger,
    find_stand_down,
    record_stand_down,
)
from maestro.standdown_validity import StanddownClaim
from maestro.vcs import PrMergeState, VCSError


class _StubConfig(SimpleNamespace):
    """Stub config carrying ``Config``'s *derived* refuted-release budget.

    The tests flip ``refutable_standdown_enabled`` on the built stub, and
    ``deliver_handoff_cfg`` reads the derived
    ``Config.active_refutation_release_cap`` — which is the union of that flag
    and the raw cap. A plain ``SimpleNamespace`` attribute would freeze the
    derivation at build time and read stale after the flip, so the stub mirrors
    the real property rather than pinning a value.
    """

    @property
    def active_refutation_release_cap(self) -> int:
        if not self.refutable_standdown_enabled:
            return 0
        return int(self.standdown_refutation_release_cap)


def _consciousness_mock() -> MagicMock:
    """Consciousness stub whose ``mutate_self_model`` runs its callback.

    The real owner hands the callback the *re-read live* model; the stub hands
    it ``load_self_model.return_value``, which is the model the test seeded —
    the same object its assertions read back.
    """
    mock = MagicMock()
    mock.mutate_self_model.side_effect = lambda fn: fn(mock.load_self_model())
    return mock


def _stub_agent(**overrides: Any) -> SimpleNamespace:
    """Minimal duck-typed agent exposing only what the controller touches.

    The narrow surface is the point of the extraction: everything the
    cluster needs is listed here, not the ~81-method ``MaestroAgent``.

    ``pr_remediation_lap_budget`` defaults to ``0`` (the standdown gate
    disabled) so the existing route tests exercise the legacy path; the
    lap-budget tests below override it.
    """
    pr_ledger_mock = MagicMock()
    pr_ledger_mock.can_attempt.return_value = True
    # The lap-budget gate sums stamped laps with the repetition-gate skip count
    # (#2783); default ``skip_count`` to a real ``0`` so the arithmetic stays
    # integer-typed (an unconfigured MagicMock would break ``laps >= budget``).
    remediation_tracker_mock = MagicMock()
    remediation_tracker_mock.skip_count.return_value = 0
    # Preflight-pass ledger backend (proposal 106): the tend-route preflight-pass
    # SHA gate reads ``should_skip`` through this seam. Default it to ``False``
    # (never skip) so the existing route tests fall through the gate exactly as
    # the JSON reader did for an empty/absent ledger.
    preflight_passes_mock = MagicMock()
    preflight_passes_mock.should_skip.return_value = False
    memory_dir = Path("/fake/mem")
    stub = SimpleNamespace(
        config=_StubConfig(
            tend_main_merge_enabled=True,
            branch_resolved_gate_enabled=True,
            branch_resolved_repetition_threshold=2,
            memory_dir=memory_dir,
            # Standing settlement reconciler master switch (proposal 097): the
            # merged-dequeue gate consults the reconciled merged set only while
            # this is on. Default on (production default) so the reconciled
            # short-circuit path is exercised; the merged-eviction tests seed an
            # empty ``settled_verdicts`` so they still fall through to the oracle.
            settlement_reconciler_enabled=True,
            pr_remediation_lap_budget=0,
            # Proposal 094: the Tend-route handoff reads this predicate to pick
            # the no-progress vs gross lap count; default off (gross count) so
            # the existing route tests keep the legacy trigger.
            progress_aware_lap_budget_active=False,
            # Refutable stand-down (proposal 135): default off (base default) so
            # every existing upkeep test sees the unchanged two-pruner path; the
            # refutation tests below override it.
            refutable_standdown_enabled=False,
            # Proposal 135: the refuted-release budget. The stub mirrors the
            # shipped default so the refutation tests below exercise the real
            # bound rather than an arbitrary test-only one.
            standdown_refutation_release_cap=2,
        ),
        memory=MagicMock(),
        # ``mutate_self_model`` (#3716) is the transactional write door the
        # tend route now takes. Apply the callback to whatever model the test
        # seeded on ``load_self_model`` so the existing assertions — which
        # inspect that same ``model`` object — still observe the append.
        consciousness=_consciousness_mock(),
        remediation_tracker=remediation_tracker_mock,
        pr_ledger=pr_ledger_mock,
        # PR-admission provenance backend (proposal 106): the tend-route writer
        # records each admission through this seam. Measurement-only — the route
        # tests do not assert on it, so a no-op mock keeps the narrow surface.
        pr_provenance=MagicMock(),
        preflight_passes=preflight_passes_mock,
        # Selected stand-down backend (proposal 106): the controller reads and
        # releases merge-truth stand-down verdicts through this seam. Bind the
        # default JSONL adapter over the same in-memory ``memory_dir`` so seeded
        # verdicts round-trip through the controller under ``mem_fs``.
        stand_down_verdicts=JsonlStandDownVerdictLedger(memory_dir),
        # Reconciled-settled pre-filter backend (proposal 097): the merged
        # dequeue gate consults ``merged_settled_prs`` before the oracle. Default
        # to an empty set so the existing merged/landed route tests fall through
        # to ``is_pr_settled`` exactly as before; the reconciled-eviction test
        # overrides it with the settled PR present.
        settled_verdicts=SimpleNamespace(
            merged_settled_prs=MagicMock(return_value=frozenset()),
        ),
        # Selected escalation-standdown backend (proposal 106): the controller
        # reads/writes durable standdowns through this seam. Bind the default
        # JSON adapter over the same in-memory ``memory_dir`` so seeded/recorded
        # standdowns round-trip through the controller under ``mem_fs``.
        escalation_standdowns=JsonEscalationLedger(memory_dir),
        # Gate-activation ledger (proposal 135): the refutation release stamps
        # one ``standdown_refuted`` row per released PR through this seam.
        gate_activations=MagicMock(),
        evolution=SimpleNamespace(
            code_host=SimpleNamespace(
                is_pr_closed=MagicMock(return_value=False),
            ),
        ),
        _vcs=MagicMock(),
        _host_or_git_pr_merged=MagicMock(return_value=False),
        _branch_merged_into_main=MagicMock(return_value=False),
        # Proposal 087: every face reads merge state through this chokepoint.
        # Default to an unsettled OPEN observation so the existing route tests
        # admit/remediate as before; the merged/landed tests override it.
        is_pr_settled=MagicMock(
            return_value=_truth("OPEN", settled=False, branch_landed=False),
        ),
        _probe_pr_merge_block=AsyncMock(return_value=(True, "NONE")),
        _notify_best_effort=AsyncMock(),
        _gate_pr_remediation_merged=MagicMock(return_value=False),
        _gate_pr_remediation_landed=MagicMock(return_value=False),
        _gate_pr_remediation_host_closed=AsyncMock(return_value=False),
        _gate_pr_remediation_ready=MagicMock(return_value=False),
        _gate_pr_remediation_stood_down=AsyncMock(return_value=False),
        _gate_pr_remediation_merge_block=AsyncMock(return_value=False),
        _gate_pr_remediation_branch_resolved=AsyncMock(return_value=False),
        _preflight_pr_remediation=AsyncMock(return_value=False),
        # Red-root on non-pure-QG laps (#4216): default to a GREEN PR so the
        # route tests pay for no checkout and no diagnosis.
        observe_pr_readiness=MagicMock(
            return_value=SimpleNamespace(ci_state="GREEN"),
        ),
        _diagnose_red_root_on_ci_red=MagicMock(),
        _record_preflight_skip_outcome=AsyncMock(),
        # Owning-plan lookup the lap-budget handoff passes to ``deliver_handoff``
        # so the merge-or-close ping can name the capability the merge lands.
        # Defaults to no plan (the generic-message path); patched ``deliver_handoff``
        # ignores it, but the keyword is evaluated eagerly at the call site.
        _plan_for_pr_number=MagicMock(return_value=None),
    )
    for name, value in overrides.items():
        setattr(stub, name, value)
    return stub


def _controller(stub: SimpleNamespace) -> PrRemediationController:
    return PrRemediationController(stub)  # type: ignore[arg-type]


# --- gate_merged: reads merge-truth through the is_pr_settled chokepoint ---


def _truth(
    state: str, *, settled: bool, branch_landed: bool, pr_number: int = 7,
) -> MergeTruth:
    """Build a :class:`MergeTruth` for the chokepoint-routing tests."""
    return MergeTruth(
        pr_number=pr_number,
        settled=settled,
        state=state,  # type: ignore[arg-type]
        branch_landed=branch_landed,
        source="host_api",
        observed_at=1.0,
    )


def test_gate_merged_evicts_merged_via_chokepoint() -> None:
    """A MERGED ``MergeTruth`` (``branch_landed``) evicts the dequeued PR."""
    stub = _stub_agent(
        is_pr_settled=MagicMock(
            return_value=_truth("MERGED", settled=True, branch_landed=True),
        ),
    )
    gated = _controller(stub).gate_merged("Remediate PR #7: t", "b7")
    assert gated is True
    stub.is_pr_settled.assert_called_once_with(7)


def test_gate_merged_keeps_open_pr() -> None:
    """An OPEN PR is not evicted (still owed remediation)."""
    stub = _stub_agent()  # default OPEN observation
    gated = _controller(stub).gate_merged("Remediate PR #7: t", "b7")
    assert gated is False
    stub.is_pr_settled.assert_called_once_with(7)


def test_gate_merged_closed_unmerged_not_evicted() -> None:
    """A CLOSED-unmerged PR is settled but NOT landed — the merged-exclusion
    gate keys on ``branch_landed``, so it does not evict here."""
    stub = _stub_agent(
        is_pr_settled=MagicMock(
            return_value=_truth("CLOSED", settled=True, branch_landed=False),
        ),
    )
    gated = _controller(stub).gate_merged("Remediate PR #7: t", "b7")
    assert gated is False


def test_gate_merged_non_remediation_goal_skips_chokepoint() -> None:
    """A goal without the ``Remediate PR #<n>:`` header never reaches the
    chokepoint."""
    stub = _stub_agent()
    gated = _controller(stub).gate_merged("do something else", None)
    assert gated is False
    stub.is_pr_settled.assert_not_called()


# --- gate_merge_block / gate_branch_resolved guards -----------------------


@pytest.mark.asyncio
async def test_gate_merge_block_non_remediation_goal_skips_probe() -> None:
    stub = _stub_agent()
    gated = await _controller(stub).gate_merge_block("not a goal", None, "external")
    assert gated is False
    stub._probe_pr_merge_block.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_merge_block_awaiting_push_runnable() -> None:
    """AWAITING_PUSH is owned (proposal 098) — one push lands the on-branch
    resolution — so the gate never skips it to Tend. It returns ``False`` (the
    request stays runnable) before recording any ``TaskRecord`` or merge-block
    deferral."""
    stub = _stub_agent()
    stub._probe_pr_merge_block = AsyncMock(
        return_value=(False, MERGE_BLOCK_AWAITING_PUSH),
    )
    gated = await _controller(stub).gate_merge_block(
        "Remediate PR #11: fix CI", "maestro/evolve/ep11", "external",
    )
    assert gated is False
    stub.memory.record_task.assert_not_called()
    stub.remediation_tracker.record_outcome.assert_not_called()


@pytest.mark.asyncio
async def test_gate_merge_block_behind_main_skips() -> None:
    """BEHIND_MAIN is Tend's mechanical catch-up merge — the sole Tend-clearable
    blocker (the ownership taxonomy leaves only it), so the gate skips it to Tend
    at zero SDK cost."""
    stub = _stub_agent()
    stub._probe_pr_merge_block = AsyncMock(
        return_value=(False, MERGE_BLOCK_BEHIND_MAIN),
    )
    gated = await _controller(stub).gate_merge_block(
        "Remediate PR #12: fix CI", "maestro/evolve/ep12", "external",
    )
    assert gated is True
    stub.remediation_tracker.record_outcome.assert_called_once_with(
        12, noop=True, merge_block=True,
    )


@pytest.mark.asyncio
async def test_gate_merge_block_behind_main_runs_under_merge_queue() -> None:
    """Under the merge-queue model Tend never catches a branch up (#4307), so a
    BEHIND_MAIN verdict is not Tend-clearable and the gate lets the lap run."""
    stub = _stub_agent()
    stub.config.tend_main_merge_enabled = False
    stub._probe_pr_merge_block = AsyncMock(
        return_value=(False, MERGE_BLOCK_BEHIND_MAIN),
    )
    gated = await _controller(stub).gate_merge_block(
        "Remediate PR #12: fix CI", "maestro/evolve/ep12", "external",
    )
    assert gated is False
    stub.remediation_tracker.record_outcome.assert_not_called()


@pytest.mark.asyncio
async def test_gate_merge_block_ci_failing_on_behind_head_runs() -> None:
    """A behind PR whose head CI is red reads CI_FAILING (#4096) — Tend refuses
    to catch up a red head, so the gate must let Tend's routed CI-failure
    remediation run rather than evicting it as Tend-clearable."""
    stub = _stub_agent()
    stub._probe_pr_merge_block = AsyncMock(
        return_value=(False, MERGE_BLOCK_CI_FAILING),
    )
    problem = _format_quality_gates_problem(
        12, f"{CI_FAILURE_ON_HEAD_SUMMARY}: browser-tests",
    )
    gated = await _controller(stub).gate_merge_block(
        f"Remediate PR #12: {problem}", "maestro/evolve/ep12", "external",
    )
    assert gated is False
    stub._probe_pr_merge_block.assert_awaited_once_with(12, "maestro/evolve/ep12")
    stub.memory.record_task.assert_not_called()
    stub.remediation_tracker.record_outcome.assert_not_called()
    stub._notify_best_effort.assert_not_awaited()


# --- _tend_clearable_blockers: the one shared, classifier-derived set (098) --


def test_tend_clearable_blockers_is_behind_main_only() -> None:
    """The set is exactly ``{BEHIND_MAIN}`` (proposal 098).

    ``AWAITING_PUSH`` is owned and never in the set — an unpushed on-branch
    resolution is the agent's own next act, not a Tend delivery to defer to. Only
    the mechanical catch-up merge remains.
    """
    assert _tend_clearable_blockers(tend_catch_up=True) == frozenset(
        {MERGE_BLOCK_BEHIND_MAIN},
    )


def test_tend_clearable_blockers_empty_under_merge_queue() -> None:
    """With the Tend catch-up merge off, nothing is Tend-clearable (#4307)."""
    assert _tend_clearable_blockers(tend_catch_up=False) == frozenset()


# --- gate_stood_down: the Tend-clearable set is the classifier's (098) ------


def _stood_down_stub(
    mem_fs: dict[str, str], *, pr: int, reason: str,
) -> SimpleNamespace:
    """A stub whose PR *pr* carries a stand-down verdict at its current head.

    Seeds the real ``(pr, head_sha)``-keyed verdict through ``mem_fs`` (no real
    file I/O) and points ``_probe_pr_merge_block`` at *reason*.
    """
    stub = _stub_agent()
    stub._vcs.rev_parse = MagicMock(return_value="sha1")
    stub._probe_pr_merge_block = AsyncMock(return_value=(False, reason))
    record_stand_down(
        pr, "sha1", JsonlStandDownVerdictLedger(stub.config.memory_dir),
        now="2026-07-20T00:00:00+00:00",
    )
    assert find_stand_down(
        pr, "sha1", JsonlStandDownVerdictLedger(stub.config.memory_dir),
    ) is not None
    return stub


@pytest.mark.asyncio
async def test_gate_stood_down_awaiting_push_releases(
    mem_fs: dict[str, str],
) -> None:
    """AWAITING_PUSH is owned (proposal 098), so a stand-down verdict premised on
    "Tend delivers the push" is no longer honest — one push lands the on-branch
    resolution. The brake releases (``False``) and the request falls through to a
    real remediation lap, recording no stand-down no-op."""
    stub = _stood_down_stub(
        mem_fs, pr=21, reason=MERGE_BLOCK_AWAITING_PUSH,
    )
    gated = await _controller(stub).gate_stood_down(
        "Remediate PR #21: fix CI", "maestro/evolve/ep21", "external",
    )
    assert gated is False
    stub.memory.record_task.assert_not_called()
    # The release arm returns ``False`` before recording anything: the
    # merge-block skip is written only on a honored eviction, never here.
    stub.remediation_tracker.record_outcome.assert_not_called()
    # The verdict itself is untouched — the gate declines to honor it, it does
    # not consume or release it (a new head is what auto-releases).
    assert find_stand_down(
        21, "sha1", JsonlStandDownVerdictLedger(stub.config.memory_dir),
    ) is not None


@pytest.mark.asyncio
async def test_gate_stood_down_ci_failing_releases(
    mem_fs: dict[str, str],
) -> None:
    """A red head on a behind PR reads CI_FAILING (#4096), which Tend never
    clears — the stand-down premise is false, so the latch releases."""
    stub = _stood_down_stub(
        mem_fs, pr=23, reason=MERGE_BLOCK_CI_FAILING,
    )
    gated = await _controller(stub).gate_stood_down(
        "Remediate PR #23: fix CI", "maestro/evolve/ep23", "external",
    )
    assert gated is False
    stub.memory.record_task.assert_not_called()
    stub.remediation_tracker.record_outcome.assert_not_called()


@pytest.mark.asyncio
async def test_gate_stood_down_behind_main_honored(
    mem_fs: dict[str, str],
) -> None:
    """BEHIND_MAIN is Tend's mechanical catch-up merge — the sole Tend-clearable
    blocker — so the stand-down verdict is honored: the zero-cost ``[stood-down]``
    ``partial`` no-op."""
    stub = _stood_down_stub(
        mem_fs, pr=22, reason=MERGE_BLOCK_BEHIND_MAIN,
    )
    gated = await _controller(stub).gate_stood_down(
        "Remediate PR #22: fix CI", "maestro/evolve/ep22", "external",
    )
    assert gated is True
    recorded = stub.memory.record_task.call_args.args[0]
    assert recorded.outcome == "partial"
    assert "[stood-down]" in recorded.result_summary


@pytest.mark.asyncio
async def test_gate_stood_down_behind_main_releases_under_merge_queue(
    mem_fs: dict[str, str],
) -> None:
    """With the Tend catch-up merge off (#4307) a "Tend delivers this" verdict
    on a behind PR is false, so the latch releases into a real lap."""
    stub = _stood_down_stub(
        mem_fs, pr=24, reason=MERGE_BLOCK_BEHIND_MAIN,
    )
    stub.config.tend_main_merge_enabled = False
    gated = await _controller(stub).gate_stood_down(
        "Remediate PR #24: fix CI", "maestro/evolve/ep24", "external",
    )
    assert gated is False
    stub.memory.record_task.assert_not_called()


@pytest.mark.asyncio
async def test_gate_stood_down_comment_goal_never_evicted(
    mem_fs: dict[str, str],
) -> None:
    """A comment/review-driven goal survives ``gate_stood_down`` (#2968).

    Even on a stood-down, still-``BEHIND_MAIN`` PR — the exact state the verdict
    is keyed to — a goal carrying a ``_COMMENT_REVIEW_PROBLEM_MARKERS`` marker is
    LLM-only work Tend PR never does, so the gate must fall through (``False``)
    instead of evicting it as a ``[stood-down]`` ``partial``. Because this gate
    is ordered ahead of ``gate_merge_block`` in the dequeue table, without the
    matching exemption here the one force-admitted reply lap would be evicted
    before it could deliver the reply and stamp the thread serviced.
    """
    stub = _stood_down_stub(
        mem_fs, pr=25, reason=MERGE_BLOCK_BEHIND_MAIN,
    )
    goal = f"Remediate PR #25: {_REPLY_COMPLETION_PROBLEM_MARKER}"
    gated = await _controller(stub).gate_stood_down(
        goal, "maestro/evolve/ep25", "external",
    )
    assert gated is False
    # No eviction: the force-admitted reply lap survives to run, so neither the
    # zero-cost ``partial`` no-op nor the merge-block skip is recorded.
    stub.memory.record_task.assert_not_called()
    stub.remediation_tracker.record_outcome.assert_not_called()
    # Pins the ordering: the marker check runs *before* the head-SHA resolution
    # and the merge-block probe (mirroring the sibling ``gate_merge_block``), so
    # a comment goal short-circuits without touching either. Moving the check
    # below the probe would call ``rev_parse`` / await ``_probe_pr_merge_block``
    # and trip these assertions.
    stub._vcs.rev_parse.assert_not_called()
    stub._probe_pr_merge_block.assert_not_awaited()
    # The verdict is untouched — the gate declines to consult it for this goal.
    assert find_stand_down(
        25, "sha1", JsonlStandDownVerdictLedger(stub.config.memory_dir),
    ) is not None


@pytest.mark.asyncio
async def test_gate_stood_down_behind_main_records_merge_block_skip(
    mem_fs: dict[str, str],
) -> None:
    """A honored stand-down eviction records a merge-block skip (#2760, #2796).

    ``gate_stood_down`` fires only on BEHIND_MAIN — the same state its sibling
    ``gate_merge_block`` handles — so a honored eviction records
    ``record_outcome(pr, noop=True, merge_block=True)``: visible to the tracker,
    routed to the merge-block counter, never the goal-blind ``should_reroute``
    outcome list. A plain no-op would arm re-mint suppression that the clean
    Tend catch-up merge releasing the verdict never resets, exiling the PR's
    still-unattempted comment/overflow work.
    """
    stub = _stood_down_stub(
        mem_fs, pr=23, reason=MERGE_BLOCK_BEHIND_MAIN,
    )
    gated = await _controller(stub).gate_stood_down(
        "Remediate PR #23: fix CI", "maestro/evolve/ep23", "external",
    )
    assert gated is True
    stub.remediation_tracker.record_outcome.assert_called_once_with(
        23, noop=True, merge_block=True,
    )


@pytest.mark.asyncio
async def test_gate_stood_down_evictions_never_arm_reroute_suppression(
    mem_fs: dict[str, str],
) -> None:
    """Repeated honored stand-down evictions never suppress the re-route (#2796).

    The mock-call assertion in
    ``test_gate_stood_down_behind_main_records_merge_block_skip`` pins the
    argument shape but cannot catch the behavioral consequence: because
    ``merge_block=True`` routes to the merge-block counter and no gate reads it,
    ``should_reroute`` stays ``True`` no matter how many times the eviction
    fires. This drives the *real* ``gate_stood_down`` against a *real*
    ``RemediationTracker`` twice (a persisted ``(pr, head_sha)`` verdict is not
    consumed on a honored eviction, so the second consult honors it too) and
    asserts that outcome directly — so a regression to a plain
    ``record_outcome(noop=True)`` (which would latch ``should_reroute`` ``False``
    at the threshold and exile the PR's still-unattempted comment/overflow work)
    is caught here, not just at the argument-shape mock. The two ``partial``
    ``TaskRecord``s still feed the lap-budget backstop (#2760/#2783), which
    remains the effective stopper for a genuinely stuck PR.
    """
    from maestro.remediation_tracker import RemediationTracker

    mem = Path("/fake/mem")
    tracker = RemediationTracker(mem, threshold=2)
    stub = _stub_agent(remediation_tracker=tracker)
    stub._vcs.rev_parse = MagicMock(return_value="sha1")
    stub._probe_pr_merge_block = AsyncMock(
        return_value=(False, MERGE_BLOCK_BEHIND_MAIN),
    )
    record_stand_down(
        24, "sha1", JsonlStandDownVerdictLedger(stub.config.memory_dir),
        now="2026-07-20T00:00:00+00:00",
    )
    controller = _controller(stub)
    goal = "Remediate PR #24: fix CI"

    assert await controller.gate_stood_down(
        goal, "maestro/evolve/ep24", "external",
    ) is True
    assert await controller.gate_stood_down(
        goal, "maestro/evolve/ep24", "external",
    ) is True

    # The evictions landed in the merge-block counter, NOT the goal-blind
    # ``should_reroute`` outcome list — even after the threshold (2) is reached,
    # the repetition gate is never armed, so the PR is not exiled from re-route.
    assert tracker.merge_block_skip_count(24) == 2
    assert tracker.should_reroute(24) is True
    # The #2783/#2760 lap-budget backstop stays reachable: each eviction still
    # wrote a ``partial`` ``TaskRecord`` the unlanded sensor counts.
    recorded = [c.args[0] for c in stub.memory.record_task.call_args_list]
    assert len(recorded) == 2
    assert all(r.outcome == "partial" for r in recorded)


@pytest.mark.asyncio
async def test_gate_branch_resolved_disabled_consults_nothing() -> None:
    """``[loop] branch_resolved_gate_enabled = false`` short-circuits."""
    stub = _stub_agent()
    stub.config.branch_resolved_gate_enabled = False
    gated = await _controller(stub).gate_branch_resolved(
        "Remediate PR #9: t", "b9", "external",
    )
    assert gated is False
    stub.memory.get_recent_tasks.assert_not_called()


# --- dequeue_gates ordering contract ---------------------------------------


@pytest.mark.asyncio
async def test_dequeue_gates_merged_short_circuits_later_gates() -> None:
    """The merged-PR exclusion runs first; later gates never fire."""
    stub = _stub_agent(
        _gate_pr_remediation_merged=MagicMock(return_value=True),
    )
    entry = PendingPRRequest(directive="Remediate PR #3: t", pr_branch="b3")
    gated = await _controller(stub).dequeue_gates(
        goal=entry.directive, pr_branch="b3", source="external", entry=entry,
    )
    assert gated is True
    stub._gate_pr_remediation_landed.assert_not_called()
    stub._gate_pr_remediation_stood_down.assert_not_awaited()
    stub._gate_pr_remediation_merge_block.assert_not_awaited()
    stub._gate_pr_remediation_branch_resolved.assert_not_awaited()
    stub._preflight_pr_remediation.assert_not_awaited()


@pytest.mark.asyncio
async def test_dequeue_gates_landed_short_circuits_later_gates() -> None:
    """The commit-ancestry landed gate (just after merged) evicts first."""
    stub = _stub_agent(
        _gate_pr_remediation_landed=MagicMock(return_value=True),
    )
    entry = PendingPRRequest(directive="Remediate PR #3: t", pr_branch="b3")
    gated = await _controller(stub).dequeue_gates(
        goal=entry.directive, pr_branch="b3", source="external", entry=entry,
    )
    assert gated is True
    stub._gate_pr_remediation_merged.assert_called_once_with(
        entry.directive, "b3",
    )
    stub._gate_pr_remediation_landed.assert_called_once_with(entry, "external")
    stub._gate_pr_remediation_stood_down.assert_not_awaited()
    stub._gate_pr_remediation_merge_block.assert_not_awaited()
    stub._gate_pr_remediation_branch_resolved.assert_not_awaited()
    stub._preflight_pr_remediation.assert_not_awaited()


@pytest.mark.asyncio
async def test_dequeue_gates_stood_down_short_circuits_later_gates() -> None:
    """The stand-down gate (just after landed) evicts before later gates fire."""
    stub = _stub_agent(
        _gate_pr_remediation_stood_down=AsyncMock(return_value=True),
    )
    entry = PendingPRRequest(directive="Remediate PR #3: t", pr_branch="b3")
    gated = await _controller(stub).dequeue_gates(
        goal=entry.directive, pr_branch="b3", source="external", entry=entry,
    )
    assert gated is True
    stub._gate_pr_remediation_merged.assert_called_once_with(
        entry.directive, "b3",
    )
    stub._gate_pr_remediation_landed.assert_called_once_with(entry, "external")
    stub._gate_pr_remediation_stood_down.assert_awaited_once_with(
        entry.directive, "b3", "external",
    )
    stub._gate_pr_remediation_merge_block.assert_not_awaited()
    stub._gate_pr_remediation_branch_resolved.assert_not_awaited()
    stub._preflight_pr_remediation.assert_not_awaited()


@pytest.mark.asyncio
async def test_dequeue_gates_dispatches_through_agent_shims() -> None:
    """Gate dispatch goes through the agent attributes (patchability seam)."""
    stub = _stub_agent()
    entry = PendingPRRequest(
        directive="Remediate PR #3: reviewer feedback", pr_branch="b3",
    )
    gated = await _controller(stub).dequeue_gates(
        goal=entry.directive, pr_branch="b3", source="external", entry=entry,
    )
    assert gated is False
    stub._gate_pr_remediation_merged.assert_called_once_with(
        entry.directive, "b3",
    )
    stub._gate_pr_remediation_landed.assert_called_once_with(entry, "external")
    stub._gate_pr_remediation_stood_down.assert_awaited_once_with(
        entry.directive, "b3", "external",
    )
    stub._gate_pr_remediation_merge_block.assert_awaited_once_with(
        entry.directive, "b3", "external",
    )
    stub._gate_pr_remediation_branch_resolved.assert_awaited_once_with(
        entry.directive, "b3", "external",
    )
    # The directive is not a pure quality-gates request, so preflight
    # never runs.
    stub._preflight_pr_remediation.assert_not_awaited()


@pytest.mark.asyncio
async def test_dequeue_gates_preflight_pass_records_success_noop() -> None:
    """A preflight pass stamps ``success`` and routes the no-op outcome."""
    stub = _stub_agent(
        _preflight_pr_remediation=AsyncMock(return_value=True),
    )
    directive = (
        "Remediate PR #5: t\n\n## Problem\n"
        "After merging origin/main into PR #5, quality gates failed: pytest."
    )
    entry = PendingPRRequest(directive=directive, pr_branch="b5")
    gated = await _controller(stub).dequeue_gates(
        goal=directive, pr_branch="b5", source="external", entry=entry,
    )
    assert gated is True
    stub._preflight_pr_remediation.assert_awaited_once_with(entry)
    record = stub.memory.record_task.call_args.args[0]
    assert record.outcome == "success"
    assert "[preflight]" in record.result_summary
    stub._record_preflight_skip_outcome.assert_awaited_once_with(
        directive, "b5",
    )


# --- unified disposition routing parity (proposal 080, Phase 4) ------------


@pytest.mark.asyncio
async def test_dequeue_gates_fired_gate_evicts() -> None:
    """A firing gate evicts through the disposition decision site.

    Drives the real :meth:`classify_dequeue` walk (a fired merge-block gate)
    through :meth:`dequeue_gates`. The run-vs-skip boolean is derived from
    ``classify_pr_remediation_disposition``, and the gate that fired runs
    exactly once — the side effects (the zero-cost ``TaskRecord``) are produced
    inside ``classify_dequeue``, not by the disposition mapping.
    """
    stub = _stub_agent(
        _gate_pr_remediation_merge_block=AsyncMock(return_value=True),
    )
    entry = PendingPRRequest(directive="Remediate PR #9: t", pr_branch="b9")
    gated = await _controller(stub).dequeue_gates(
        goal=entry.directive, pr_branch="b9", source="external", entry=entry,
    )
    assert gated is True
    stub._gate_pr_remediation_merge_block.assert_awaited_once_with(
        entry.directive, "b9", "external",
    )
    # The merge-block gate fired, so the gates after it never run.
    stub._gate_pr_remediation_branch_resolved.assert_not_awaited()
    stub._preflight_pr_remediation.assert_not_awaited()


@pytest.mark.asyncio
async def test_dequeue_gates_no_gate_fires_runs() -> None:
    """A REMEDIABLE request becomes a real task.

    With no gate firing ``classify_dequeue`` returns ``REMEDIABLE``, which maps
    to ``RUN`` alone, so ``dequeue_gates`` returns ``False`` (run, not evict).
    """
    stub = _stub_agent()
    entry = PendingPRRequest(
        directive="Remediate PR #9: reviewer feedback", pr_branch="b9",
    )
    gated = await _controller(stub).dequeue_gates(
        goal=entry.directive, pr_branch="b9", source="external", entry=entry,
    )
    assert gated is False


# --- classify_dequeue transition table (proposal 070, step 2) --------------

# A directive whose only problem is a post-merge quality-gates failure — the
# one class ``_is_pure_quality_gates_directive`` admits to the preflight gate.
_PURE_QG_DIRECTIVE = (
    "Remediate PR #5: t\n\n## Problem\n"
    "After merging origin/main into PR #5, quality gates failed: pytest."
)


def _ctx(
    directive: str = "Remediate PR #3: reviewer feedback",
    pr_branch: str = "b3",
) -> _DequeueContext:
    """Build a ``_DequeueContext`` for a popped PR-remediation request."""
    return _DequeueContext(
        goal=directive,
        pr_branch=pr_branch,
        source="external",
        entry=PendingPRRequest(directive=directive, pr_branch=pr_branch),
    )


def test_dequeue_gate_table_order_is_pinned() -> None:
    """The transition-table order is the load-bearing dequeue chain; a reorder
    must fail this guard (proposal 070, step 2)."""
    states = [state for state, _ in _DEQUEUE_GATE_TABLE]
    assert states == [
        PrRemediationState.MERGED,
        PrRemediationState.LANDED,
        PrRemediationState.HOST_CLOSED_UNMERGED,
        PrRemediationState.READY_AND_WAITING,
        PrRemediationState.STOOD_DOWN,
        PrRemediationState.MERGE_BLOCKED,
        PrRemediationState.BRANCH_RESOLVED,
        PrRemediationState.PREFLIGHT_CLEAN,
    ]
    # The acceptance invariant: merged-exclusion precedes the commit-ancestry
    # landed gate, which precedes merge-block precedes branch-resolved precedes
    # preflight. The host-closed gate slots immediately after landed (a human
    # CLOSED-unmerged terminal the mergedAt-keyed arms miss); the observed
    # ready-and-waiting gate and the keyed stand-down gate slot between it and
    # merge-block without disturbing that relative order, with the
    # freshly-observed [ready] fact ahead of the stale verdict.
    assert states.index(PrRemediationState.MERGED) < states.index(
        PrRemediationState.LANDED,
    )
    assert states.index(PrRemediationState.LANDED) < states.index(
        PrRemediationState.HOST_CLOSED_UNMERGED,
    )
    assert states.index(PrRemediationState.HOST_CLOSED_UNMERGED) < states.index(
        PrRemediationState.READY_AND_WAITING,
    )
    assert states.index(PrRemediationState.LANDED) < states.index(
        PrRemediationState.READY_AND_WAITING,
    )
    assert states.index(PrRemediationState.READY_AND_WAITING) < states.index(
        PrRemediationState.STOOD_DOWN,
    )
    assert states.index(PrRemediationState.LANDED) < states.index(
        PrRemediationState.MERGE_BLOCKED,
    )
    assert states.index(PrRemediationState.MERGED) < states.index(
        PrRemediationState.MERGE_BLOCKED,
    )
    assert states.index(PrRemediationState.MERGE_BLOCKED) < states.index(
        PrRemediationState.BRANCH_RESOLVED,
    )
    assert states.index(PrRemediationState.BRANCH_RESOLVED) < states.index(
        PrRemediationState.PREFLIGHT_CLEAN,
    )


@pytest.mark.asyncio
async def test_classify_dequeue_evaluates_gates_in_table_order() -> None:
    """``classify_dequeue`` consults the gates in ``_DEQUEUE_GATE_TABLE`` order:
    merged → landed → host-closed → ready → stand-down → merge-block →
    branch-resolved → preflight."""
    calls: list[str] = []

    def _sync(name: str) -> Any:
        def _fn(*_a: Any, **_k: Any) -> bool:
            calls.append(name)
            return False
        return _fn

    def _async(name: str) -> Any:
        async def _fn(*_a: Any, **_k: Any) -> bool:
            calls.append(name)
            return False
        return _fn

    stub = _stub_agent(
        _gate_pr_remediation_merged=_sync("merged"),
        _gate_pr_remediation_landed=_sync("landed"),
        _gate_pr_remediation_host_closed=_async("host_closed"),
        _gate_pr_remediation_ready=_sync("ready"),
        _gate_pr_remediation_stood_down=_async("stood_down"),
        _gate_pr_remediation_merge_block=_async("merge_block"),
        _gate_pr_remediation_branch_resolved=_async("branch_resolved"),
        # A pure quality-gates directive reaches the preflight gate, which
        # consults ``_preflight_pr_remediation`` — record it there.
        _preflight_pr_remediation=_async("preflight"),
    )
    state = await _controller(stub).classify_dequeue(_ctx(_PURE_QG_DIRECTIVE, "b5"))
    assert state is PrRemediationState.REMEDIABLE
    assert calls == [
        "merged", "landed", "host_closed", "ready", "stood_down",
        "merge_block", "branch_resolved", "preflight",
    ]


@pytest.mark.asyncio
async def test_classify_dequeue_landed_returns_landed_state() -> None:
    stub = _stub_agent(
        _gate_pr_remediation_landed=MagicMock(return_value=True),
    )
    state = await _controller(stub).classify_dequeue(_ctx())
    assert state is PrRemediationState.LANDED


@pytest.mark.asyncio
async def test_classify_dequeue_host_closed_returns_host_closed_state() -> None:
    stub = _stub_agent(
        _gate_pr_remediation_host_closed=AsyncMock(return_value=True),
    )
    state = await _controller(stub).classify_dequeue(_ctx())
    assert state is PrRemediationState.HOST_CLOSED_UNMERGED


@pytest.mark.asyncio
async def test_classify_dequeue_ready_returns_ready_state() -> None:
    stub = _stub_agent(
        _gate_pr_remediation_ready=MagicMock(return_value=True),
    )
    state = await _controller(stub).classify_dequeue(_ctx())
    assert state is PrRemediationState.READY_AND_WAITING


@pytest.mark.asyncio
async def test_classify_dequeue_merged_returns_merged_state() -> None:
    stub = _stub_agent(_gate_pr_remediation_merged=MagicMock(return_value=True))
    state = await _controller(stub).classify_dequeue(_ctx())
    assert state is PrRemediationState.MERGED


@pytest.mark.asyncio
async def test_classify_dequeue_stood_down_returns_stood_down_state() -> None:
    stub = _stub_agent(
        _gate_pr_remediation_stood_down=AsyncMock(return_value=True),
    )
    state = await _controller(stub).classify_dequeue(_ctx())
    assert state is PrRemediationState.STOOD_DOWN


@pytest.mark.asyncio
async def test_classify_dequeue_merge_block_returns_merge_blocked_state() -> None:
    stub = _stub_agent(
        _gate_pr_remediation_merge_block=AsyncMock(return_value=True),
    )
    state = await _controller(stub).classify_dequeue(_ctx())
    assert state is PrRemediationState.MERGE_BLOCKED


@pytest.mark.asyncio
async def test_classify_dequeue_branch_resolved_returns_branch_resolved_state() -> None:
    stub = _stub_agent(
        _gate_pr_remediation_branch_resolved=AsyncMock(return_value=True),
    )
    state = await _controller(stub).classify_dequeue(_ctx())
    assert state is PrRemediationState.BRANCH_RESOLVED


@pytest.mark.asyncio
async def test_classify_dequeue_preflight_pass_returns_preflight_clean_state() -> None:
    stub = _stub_agent(_preflight_pr_remediation=AsyncMock(return_value=True))
    state = await _controller(stub).classify_dequeue(
        _ctx(_PURE_QG_DIRECTIVE, "b5"),
    )
    assert state is PrRemediationState.PREFLIGHT_CLEAN


@pytest.mark.asyncio
async def test_classify_dequeue_no_gate_returns_remediable() -> None:
    """A genuine remediation (reviewer feedback) fires no gate — it runs."""
    stub = _stub_agent()
    state = await _controller(stub).classify_dequeue(_ctx())
    assert state is PrRemediationState.REMEDIABLE
    # The preflight runner short-circuits on the non-pure directive: the merged
    # tree is never probed and no zero-cost record is stamped.
    stub._preflight_pr_remediation.assert_not_awaited()
    stub.memory.record_task.assert_not_called()


@pytest.mark.asyncio
async def test_classify_dequeue_pure_directive_failing_preflight_is_remediable() -> None:
    """A pure quality-gates directive whose *merged* tree still fails the gates
    falls through the preflight runner — the request stays remediable and no
    zero-cost ``[preflight]`` record is stamped."""
    stub = _stub_agent()  # _preflight_pr_remediation defaults to False
    state = await _controller(stub).classify_dequeue(
        _ctx(_PURE_QG_DIRECTIVE, "b5"),
    )
    assert state is PrRemediationState.REMEDIABLE
    stub._preflight_pr_remediation.assert_awaited_once()
    stub.memory.record_task.assert_not_called()


# --- red-root on every admitted CI-red lap (#4216) --------------------------

# Tend's pre-merge CI-red route: no ``, quality gates failed:`` marker, so the
# directive is *not* pure QG and never reaches ``preflight``.
_PRE_MERGE_CI_DIRECTIVE = (
    "Remediate PR #7: t\n\n## Problem\n"
    + _format_quality_gates_problem(7, f"{CI_FAILURE_ON_HEAD_SUMMARY}: quality")
)


def _red_root_stub(ci_state: str = "FAILING", **overrides: Any) -> SimpleNamespace:
    """A stub whose workspace sits clean on ``main`` and whose PR reads *ci_state*."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.porcelain_status.return_value = ""
    stub = _stub_agent(
        _vcs=vcs,
        observe_pr_readiness=MagicMock(
            return_value=SimpleNamespace(ci_state=ci_state),
        ),
    )
    for name, value in overrides.items():
        setattr(stub, name, value)
    return stub


@pytest.mark.asyncio
async def test_dequeue_runs_red_root_on_pre_merge_ci_failure_on_head() -> None:
    """A pre-merge CI-red lap gets the diagnosis once, on the PR head."""
    stub = _red_root_stub()
    positioned: list[str] = []

    def _diagnose(_req: PendingPRRequest) -> None:
        positioned.append(stub._vcs.checkout.call_args.args[0])

    stub._diagnose_red_root_on_ci_red = MagicMock(side_effect=_diagnose)
    state = await _controller(stub).classify_dequeue(
        _ctx(_PRE_MERGE_CI_DIRECTIVE, "b7"),
    )
    assert state is PrRemediationState.REMEDIABLE
    stub._diagnose_red_root_on_ci_red.assert_called_once()
    assert positioned == ["b7"]
    # Restored to the entry branch afterwards; the preflight never ran.
    assert stub._vcs.checkout.call_args.args[0] == "main"
    stub._preflight_pr_remediation.assert_not_awaited()


@pytest.mark.asyncio
async def test_dequeue_red_root_skipped_on_green_pr() -> None:
    stub = _red_root_stub("GREEN")
    state = await _controller(stub).classify_dequeue(
        _ctx(_PRE_MERGE_CI_DIRECTIVE, "b7"),
    )
    assert state is PrRemediationState.REMEDIABLE
    stub._vcs.checkout.assert_not_called()
    stub._diagnose_red_root_on_ci_red.assert_not_called()


@pytest.mark.asyncio
async def test_dequeue_red_root_skipped_without_header() -> None:
    stub = _red_root_stub()
    state = await _controller(stub).classify_dequeue(_ctx("no header", "b7"))
    assert state is PrRemediationState.REMEDIABLE
    stub.observe_pr_readiness.assert_not_called()
    stub._diagnose_red_root_on_ci_red.assert_not_called()


@pytest.mark.asyncio
async def test_dequeue_red_root_skipped_on_dirty_workspace() -> None:
    stub = _red_root_stub()
    stub._vcs.porcelain_status.return_value = " M maestro/x.py\n"
    state = await _controller(stub).classify_dequeue(
        _ctx(_PRE_MERGE_CI_DIRECTIVE, "b7"),
    )
    assert state is PrRemediationState.REMEDIABLE
    stub._vcs.checkout.assert_not_called()
    stub._diagnose_red_root_on_ci_red.assert_not_called()


@pytest.mark.asyncio
async def test_dequeue_red_root_skipped_on_unreadable_workspace() -> None:
    stub = _red_root_stub()
    stub._vcs.get_current_branch.side_effect = VCSError("boom")
    state = await _controller(stub).classify_dequeue(
        _ctx(_PRE_MERGE_CI_DIRECTIVE, "b7"),
    )
    assert state is PrRemediationState.REMEDIABLE
    stub._diagnose_red_root_on_ci_red.assert_not_called()


@pytest.mark.asyncio
async def test_dequeue_red_root_skipped_on_failed_checkout() -> None:
    stub = _red_root_stub()

    def _checkout(branch: str) -> None:
        if branch != "main":
            raise VCSError(f"no {branch}")

    stub._vcs.checkout.side_effect = _checkout
    state = await _controller(stub).classify_dequeue(
        _ctx(_PRE_MERGE_CI_DIRECTIVE, "b7"),
    )
    assert state is PrRemediationState.REMEDIABLE
    stub._diagnose_red_root_on_ci_red.assert_not_called()
    assert stub._vcs.checkout.call_args.args[0] == "main"


@pytest.mark.asyncio
async def test_dequeue_red_root_falls_back_to_origin_checkout() -> None:
    stub = _red_root_stub()
    stub._vcs.checkout.side_effect = [VCSError("no local b7"), None, None]
    await _controller(stub).classify_dequeue(_ctx(_PRE_MERGE_CI_DIRECTIVE, "b7"))
    stub._vcs.checkout_new_branch.assert_called_once_with("b7")
    stub._diagnose_red_root_on_ci_red.assert_called_once()


@pytest.mark.asyncio
async def test_dequeue_red_root_on_pr_branch_does_not_move_worktree() -> None:
    stub = _red_root_stub()
    stub._vcs.get_current_branch.return_value = "b7"
    stub._vcs.porcelain_status.return_value = " M x\n"
    await _controller(stub).classify_dequeue(_ctx(_PRE_MERGE_CI_DIRECTIVE, "b7"))
    stub._vcs.checkout.assert_not_called()
    stub._diagnose_red_root_on_ci_red.assert_called_once()


@pytest.mark.asyncio
async def test_dequeue_red_root_failure_is_loud_and_restores() -> None:
    stub = _red_root_stub(
        _diagnose_red_root_on_ci_red=MagicMock(side_effect=RuntimeError("x")),
    )
    with pytest.raises(RedRootDiagnosisError):
        await _controller(stub).classify_dequeue(
            _ctx(_PRE_MERGE_CI_DIRECTIVE, "b7"),
        )
    assert stub._vcs.checkout.call_args.args[0] == "main"


@pytest.mark.asyncio
async def test_dequeue_pure_qg_directive_diagnoses_only_inside_preflight() -> None:
    """The pure-QG route keeps its in-``preflight`` diagnosis — not a second one."""
    stub = _red_root_stub()
    await _controller(stub).classify_dequeue(_ctx(_PURE_QG_DIRECTIVE, "b5"))
    stub._diagnose_red_root_on_ci_red.assert_not_called()
    stub._preflight_pr_remediation.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("directive", [
    _PURE_QG_DIRECTIVE,
    _PRE_MERGE_CI_DIRECTIVE,
    "Remediate PR #7: t\n\n## Problem\n"
    + _format_conflict_problem(7, ["maestro/x.py"]),
    "Remediate PR #7: t\n\n## Problem\n" + _format_review_feedback_problem(7),
    "Remediate PR #7: t\n\n## Problem\n"
    + _format_bug_comments_problem(7, [(1, "maestro/x.py", "bug", "bug")]),
], ids=[
    "post-merge-qg", "ci-failure-on-head", "conflict", "review-feedback",
    "bug-comments",
])
async def test_every_routed_directive_gets_exactly_one_diagnosis(
    directive: str,
) -> None:
    """Structural guard: every Tend-routed directive class, built by its real
    formatter, reaches the diagnosis exactly once through ``classify_dequeue``
    on a FAILING PR — a filter reorder that drops or doubles the dequeue call
    fails here (#4216). The pure-QG case stubs ``preflight`` with a mirror of
    its in-body call; that the real preflight makes the call is pinned by
    ``tests/test_red_root.py``'s preflight ordering tests."""
    stub = _red_root_stub()
    calls: list[str] = []
    stub._diagnose_red_root_on_ci_red = MagicMock(
        side_effect=lambda _r: calls.append("dequeue"),
    )

    async def _preflight(req: PendingPRRequest) -> bool:
        # Mirror the real preflight's in-body diagnosis call.
        stub._diagnose_red_root_on_ci_red(req)
        return False

    stub._preflight_pr_remediation = _preflight
    await _controller(stub).classify_dequeue(_ctx(directive, "b7"))
    assert len(calls) == 1


# --- preflight / route_feedback guards --------------------------------------


@pytest.mark.asyncio
async def test_preflight_without_branch_returns_false() -> None:
    """No ``pr_branch`` → no subprocess work, immediate False."""
    stub = _stub_agent()
    entry = PendingPRRequest(directive="Remediate PR #2: t", pr_branch=None)
    result = await _controller(stub).preflight(entry)
    assert result is False


def test_route_feedback_empty_tend_result_is_noop() -> None:
    """No routed categories → the self-model is never loaded or saved."""
    stub = _stub_agent()
    _controller(stub).route_feedback(TendPRsResult())
    stub.consciousness.load_self_model.assert_not_called()
    stub.consciousness.mutate_self_model.assert_not_called()


def test_route_feedback_ledger_exhausted_skips_routing() -> None:
    """PR-ledger gate: a PR whose remediation budget is exhausted is not
    re-routed — the logger INFO fires and the pending queue stays empty.
    Uses a CHANGES_REQUESTED route: a GitHub-only signal that earns no
    fresh-budget grant (unlike a conflict route)."""
    from unittest.mock import MagicMock

    pr_ledger_mock = MagicMock()
    pr_ledger_mock.can_attempt.return_value = False
    stub = _stub_agent(pr_ledger=pr_ledger_mock)
    model = MagicMock()
    model.pending_pr_requests = []
    model.pending_user_requests = []
    stub.consciousness.load_self_model.return_value = model
    stub.remediation_tracker.should_reroute.return_value = True
    stub._vcs.rev_parse.return_value = ""

    result = TendPRsResult()
    result.changes_requested_prs.append((
        10, "address review",
        "plan-x", "pr body", "https://gh/pr/10", "maestro/evolve/plan-x",
    ))
    _controller(stub).route_feedback(result)
    # Budget exhausted — PR must not appear in pending_pr_requests.
    assert model.pending_pr_requests == []
    stub.consciousness.mutate_self_model.assert_not_called()
    pr_ledger_mock.grant_fresh_attempts.assert_not_called()


def test_route_feedback_conflict_grants_fresh_ledger_budget() -> None:
    """A conflict route is a genuine branch-state change: the controller
    grants a fresh ledger budget alongside the tracker reset, ahead of the
    can_attempt gate — otherwise a budget-exhausted PR with a new conflict
    could never recover autonomously. (End-to-end unblocking against the
    real ledger is covered in ``tests/test_pr_feedback_routing.py``.)"""
    from unittest.mock import MagicMock

    pr_ledger_mock = MagicMock()
    pr_ledger_mock.can_attempt.return_value = True
    stub = _stub_agent(pr_ledger=pr_ledger_mock)
    model = MagicMock()
    model.pending_pr_requests = []
    model.pending_user_requests = []
    stub.consciousness.load_self_model.return_value = model
    # Block at the (later) repetition gate so the dispatch-seam assertion
    # needs no real plan/memory plumbing behind the append path.
    stub.remediation_tracker.should_reroute.return_value = False

    result = TendPRsResult()
    result.conflicts_routed.append((
        10, "fix conflicts", ["file.py"],
        "plan-x", "pr body", "https://gh/pr/10", "maestro/evolve/plan-x",
    ))
    _controller(stub).route_feedback(result)
    stub.remediation_tracker.reset.assert_called_once_with(10)
    grant_args = pr_ledger_mock.grant_fresh_attempts.call_args
    assert grant_args is not None
    assert grant_args.args == (10,)


# --- admission-time owned-blocker tagging (#2863) ---------------------------


def _conflict_result(pr: int = 10) -> TendPRsResult:
    """One PR routed through tend's live conflict-detection signal."""
    result = TendPRsResult()
    result.conflicts_routed.append((
        pr, "fix conflicts", ["file.py"],
        "plan-x", "pr body", f"https://gh/pr/{pr}", "maestro/evolve/plan-x",
    ))
    return result


def test_route_feedback_conflict_admits_remediation() -> None:
    """A conflict-routed PR is admitted as an ordinary remediation entry.

    The #2863 owned-blocker admission tag was retired with the owned-relap
    divert (proposal 092): a CONFLICTING lineage now routes as a plain
    remediation task — the ``wip_primacy`` gate names the PR anyway — so the
    entry carries only its directive and PR branch, no divert tag."""
    stub = _stub_agent()  # lap budget disabled
    model = MagicMock()
    model.pending_pr_requests = []
    model.pending_user_requests = []
    stub.consciousness.load_self_model.return_value = model
    stub.remediation_tracker.should_reroute.return_value = True
    stub._vcs.rev_parse.return_value = ""
    stub.memory.get_plan.return_value = None

    _controller(stub).route_feedback(_conflict_result())

    assert len(model.pending_pr_requests) == 1
    entry = model.pending_pr_requests[0]
    assert "Remediate PR #10" in entry.directive
    # The retired owned-blocker tag is gone from the dataclass entirely.
    assert not hasattr(entry, "owned_blocker")


def test_route_feedback_conflict_dedups_already_pending_entry() -> None:
    """A conflict-routed PR already queued is deduped, not re-enqueued.

    ``_pr_already_pending`` ``continue``s on the dedup marker in the queued
    directive. With the owned-blocker in-place upgrade retired (proposal 092),
    a pure-dedup pass appends nothing and fires no durable save."""
    stub = _stub_agent()  # lap budget disabled
    model = MagicMock()
    # PR #10 already queued with the dedup marker in its directive so
    # ``_pr_already_pending`` hits.
    existing = PendingPRRequest(
        directive="Remediate PR #10: address earlier review",
        pr_branch="maestro/evolve/plan-x",
    )
    model.pending_pr_requests = [existing]
    model.pending_user_requests = []
    stub.consciousness.load_self_model.return_value = model
    stub.remediation_tracker.should_reroute.return_value = True
    stub._vcs.rev_parse.return_value = ""
    stub.memory.get_plan.return_value = None

    _controller(stub).route_feedback(_conflict_result())

    # No duplicate enqueued; nothing fresh to persist.
    assert len(model.pending_pr_requests) == 1
    assert model.pending_pr_requests[0] is existing
    stub.consciousness.mutate_self_model.assert_not_called()


# --- lap-budget standdown gate (proposal 080 Phase 3) -----------------------


def _changes_requested_result(pr: int = 10) -> TendPRsResult:
    result = TendPRsResult()
    result.changes_requested_prs.append((
        pr, "address review",
        "plan-x", "pr body", f"https://gh/pr/{pr}", "maestro/evolve/plan-x",
    ))
    return result


def test_route_feedback_active_standdown_suppresses_minting() -> None:
    """A PR under an active standdown is not re-admitted (proposal 080 P3)."""
    stub = _stub_agent()
    stub.config.pr_remediation_lap_budget = 2
    model = MagicMock()
    model.pending_pr_requests = []
    model.pending_user_requests = []
    stub.consciousness.load_self_model.return_value = model
    stub.remediation_tracker.should_reroute.return_value = True
    stub._vcs.rev_parse.return_value = "head1"

    with patch(
        "maestro.loop.pr_remediation.measure_unlanded_remediations",
        return_value={10: 9},
    ), patch(
        "maestro.loop.pr_remediation.escalation_bridge."
        "prune_resolved_standdowns",
    ), patch(
        "maestro.loop.pr_remediation.escalation_bridge.has_standdown",
        return_value=True,
    ), patch(
        "maestro.loop.pr_remediation.escalation_bridge.deliver_handoff",
    ) as deliver:
        _controller(stub).route_feedback(_changes_requested_result())

    assert model.pending_pr_requests == []
    stub.consciousness.mutate_self_model.assert_not_called()
    # An active standdown short-circuits before any (re-)delivery.
    deliver.assert_not_called()


def test_route_feedback_budget_reached_delivers_and_suppresses() -> None:
    """Reaching the lap budget delivers one handoff and suppresses minting."""
    stub = _stub_agent()
    stub.config.pr_remediation_lap_budget = 2
    model = MagicMock()
    model.pending_pr_requests = []
    model.pending_user_requests = []
    stub.consciousness.load_self_model.return_value = model
    stub.remediation_tracker.should_reroute.return_value = True
    stub._vcs.rev_parse.return_value = "head1"

    with patch(
        "maestro.loop.pr_remediation.measure_unlanded_remediations",
        return_value={10: 2},
    ), patch(
        "maestro.loop.pr_remediation.escalation_bridge."
        "prune_resolved_standdowns",
    ), patch(
        "maestro.loop.pr_remediation.escalation_bridge.has_standdown",
        return_value=False,
    ), patch(
        "maestro.loop.pr_remediation.escalation_bridge.deliver_handoff",
        return_value=True,
    ) as deliver:
        _controller(stub).route_feedback(_changes_requested_result())

    assert model.pending_pr_requests == []
    stub.consciousness.mutate_self_model.assert_not_called()
    deliver.assert_called_once()
    handoff = deliver.call_args.args[1]
    assert handoff.pr_number == 10
    assert handoff.stamp_count == 2
    assert handoff.head_sha == "head1"


def test_route_feedback_progress_aware_converging_pr_not_handed_off() -> None:
    """Proposal 094: with the progress-aware predicate active, a PR whose gross
    unlanded laps reach the budget but whose consecutive no-progress laps stay
    below it is advancing readiness — it earns no handoff."""
    stub = _stub_agent()
    stub.config.pr_remediation_lap_budget = 3
    stub.config.progress_aware_lap_budget_active = True
    model = MagicMock()
    model.pending_pr_requests = []
    model.pending_user_requests = []
    stub.consciousness.load_self_model.return_value = model
    stub.remediation_tracker.should_reroute.return_value = True
    stub._vcs.rev_parse.return_value = "head1"
    stub.memory.get_plan.return_value = None

    with patch(
        "maestro.loop.pr_remediation.measure_unlanded_remediations",
        return_value={10: 9},
    ), patch(
        "maestro.loop.pr_remediation.measure_no_progress_laps",
        return_value={10: 1},
    ) as no_progress, patch(
        "maestro.loop.pr_remediation.escalation_bridge."
        "prune_resolved_standdowns",
    ), patch(
        "maestro.loop.pr_remediation.escalation_bridge.has_standdown",
        return_value=False,
    ), patch(
        "maestro.loop.pr_remediation.escalation_bridge.deliver_handoff",
        return_value=True,
    ) as deliver:
        _controller(stub).route_feedback(_changes_requested_result())

    # The no-progress count (1) + skip_count (0) is below the budget (3), so
    # despite 9 gross unlanded laps the converging PR is never handed off — it
    # routes on the normal path instead.
    no_progress.assert_called_once()
    deliver.assert_not_called()
    assert len(model.pending_pr_requests) == 1


def test_route_feedback_progress_aware_stuck_pr_handed_off_once() -> None:
    """Proposal 094: with the predicate active, a PR whose consecutive
    no-progress laps reach the budget earns exactly one merge-or-close handoff —
    the no-progress count, not the gross count, arms it."""
    stub = _stub_agent()
    stub.config.pr_remediation_lap_budget = 2
    stub.config.progress_aware_lap_budget_active = True
    model = MagicMock()
    model.pending_pr_requests = []
    model.pending_user_requests = []
    stub.consciousness.load_self_model.return_value = model
    stub.remediation_tracker.should_reroute.return_value = True
    stub._vcs.rev_parse.return_value = "head1"

    with patch(
        "maestro.loop.pr_remediation.measure_unlanded_remediations",
        return_value={10: 9},
    ), patch(
        "maestro.loop.pr_remediation.measure_no_progress_laps",
        return_value={10: 2},
    ) as no_progress, patch(
        "maestro.loop.pr_remediation.escalation_bridge."
        "prune_resolved_standdowns",
    ), patch(
        "maestro.loop.pr_remediation.escalation_bridge.has_standdown",
        return_value=False,
    ), patch(
        "maestro.loop.pr_remediation.escalation_bridge.deliver_handoff",
        return_value=True,
    ) as deliver:
        _controller(stub).route_feedback(_changes_requested_result())

    no_progress.assert_called_once()
    deliver.assert_called_once()
    handoff = deliver.call_args.args[1]
    assert handoff.pr_number == 10
    # Armed on the no-progress count (2), not the gross count (9).
    assert handoff.stamp_count == 2
    assert model.pending_pr_requests == []


def test_route_feedback_progress_aware_off_uses_gross_count() -> None:
    """Flag-off parity: with the predicate inactive the no-progress sensor is
    never measured and the gross unlanded count arms the handoff exactly as
    before (``test_route_feedback_budget_reached_delivers_and_suppresses``)."""
    stub = _stub_agent()
    stub.config.pr_remediation_lap_budget = 2
    # progress_aware_lap_budget_active stays False (default).
    model = MagicMock()
    model.pending_pr_requests = []
    model.pending_user_requests = []
    stub.consciousness.load_self_model.return_value = model
    stub.remediation_tracker.should_reroute.return_value = True
    stub._vcs.rev_parse.return_value = "head1"

    with patch(
        "maestro.loop.pr_remediation.measure_unlanded_remediations",
        return_value={10: 2},
    ), patch(
        "maestro.loop.pr_remediation.measure_no_progress_laps",
    ) as no_progress, patch(
        "maestro.loop.pr_remediation.escalation_bridge."
        "prune_resolved_standdowns",
    ), patch(
        "maestro.loop.pr_remediation.escalation_bridge.has_standdown",
        return_value=False,
    ), patch(
        "maestro.loop.pr_remediation.escalation_bridge.deliver_handoff",
        return_value=True,
    ) as deliver:
        _controller(stub).route_feedback(_changes_requested_result())

    # The sensor is never even measured when the predicate is off.
    no_progress.assert_not_called()
    deliver.assert_called_once()
    handoff = deliver.call_args.args[1]
    assert handoff.stamp_count == 2


def test_route_feedback_below_budget_admits_normally() -> None:
    """Below budget with no standdown, the PR routes on the normal path."""
    stub = _stub_agent()
    stub.config.pr_remediation_lap_budget = 5
    model = MagicMock()
    model.pending_pr_requests = []
    model.pending_user_requests = []
    stub.consciousness.load_self_model.return_value = model
    stub.remediation_tracker.should_reroute.return_value = True
    stub._vcs.rev_parse.return_value = ""
    stub.memory.get_plan.return_value = None

    with patch(
        "maestro.loop.pr_remediation.measure_unlanded_remediations",
        return_value={10: 2},
    ), patch(
        "maestro.loop.pr_remediation.escalation_bridge."
        "prune_resolved_standdowns",
    ), patch(
        "maestro.loop.pr_remediation.escalation_bridge.has_standdown",
        return_value=False,
    ), patch(
        "maestro.loop.pr_remediation.escalation_bridge.deliver_handoff",
    ) as deliver:
        _controller(stub).route_feedback(_changes_requested_result())

    deliver.assert_not_called()
    assert len(model.pending_pr_requests) == 1
    assert "Remediate PR #10" in model.pending_pr_requests[0].directive
    stub.consciousness.mutate_self_model.assert_called_once()


def test_route_feedback_budget_disabled_bypasses_standdown_gate() -> None:
    """Budget 0 bypasses the lap-budget gate — no resolved-prune, sensor, or
    standdown read — but the age-based expiry prune still runs (proposal 090):
    its writer is gated on ``phantom_escalation_delivery_enabled``, not the lap
    budget, so decoupling the expiry prune is the only path that clears an
    issue-keyed claimed-escalation standdown when ``lap_budget == 0``."""
    stub = _stub_agent()  # pr_remediation_lap_budget defaults to 0
    model = MagicMock()
    model.pending_pr_requests = []
    model.pending_user_requests = []
    stub.consciousness.load_self_model.return_value = model
    stub.remediation_tracker.should_reroute.return_value = True
    stub._vcs.rev_parse.return_value = ""
    stub.memory.get_plan.return_value = None

    with patch(
        "maestro.loop.pr_remediation.escalation_bridge."
        "prune_resolved_standdowns",
    ) as prune, patch(
        "maestro.loop.pr_remediation.escalation_bridge."
        "prune_expired_standdowns",
    ) as expiry_prune, patch(
        "maestro.loop.pr_remediation.escalation_bridge.has_standdown",
    ) as has_sd, patch(
        "maestro.loop.pr_remediation.measure_unlanded_remediations",
    ) as measure:
        _controller(stub).route_feedback(_changes_requested_result())

    prune.assert_not_called()
    has_sd.assert_not_called()
    measure.assert_not_called()
    # The age-based expiry prune is decoupled from the lap budget and runs
    # every tend pass regardless of ``lap_budget``.
    expiry_prune.assert_called_once()
    # The PR routes normally on the legacy path.
    assert len(model.pending_pr_requests) == 1


def test_route_feedback_budget_disabled_clears_expired_claimed_escalation(
    mem_fs: dict[str, str],
) -> None:
    """Regression (PR #2908): with ``lap_budget == 0`` the age-based expiry
    prune still clears an expired issue-keyed claimed-escalation standdown.

    ``deliver_claimed_escalation`` (proposal 090) is gated on
    ``phantom_escalation_delivery_enabled``, not the lap budget, so an
    ``expires_at`` prune nested under ``lap_budget > 0`` would never fire for a
    delivery-enabled + budget-0 deployment — the plain-issue standdown could
    never be released and would suppress minting forever while the ledger grew
    unbounded. Drive an empty tend pass over a real in-memory ledger and confirm
    the expired issue-keyed record is gone while an ``expires_at``-less PR record
    survives (its merge/close-only release is untouched)."""
    stub = _stub_agent()  # pr_remediation_lap_budget defaults to 0
    ledger_path = stub.config.memory_dir / "escalation_handoffs.json"
    # A definitely-past ``expires_at`` so the record is expired against any real
    # ``datetime.now`` the prune reads — no wall-clock coupling.
    mem_fs[str(ledger_path)] = json.dumps({
        "555": {
            "head_sha": "",
            "delivered_at": "2000-01-01T00:00:00+00:00",
            "serviced_comment_ids": [],
            "expires_at": "2000-01-31T00:00:00+00:00",
        },
        "10": {
            "head_sha": "abc12345",
            "delivered_at": "2000-01-01T00:00:00+00:00",
            "serviced_comment_ids": [],
        },
    })

    _controller(stub).route_feedback(TendPRsResult())

    surviving = json.loads(mem_fs[str(ledger_path)])
    # The expired issue-keyed standdown is cleared; the expiry-less PR record
    # keeps its merge/close-only release and survives the age prune.
    assert "555" not in surviving
    assert "10" in surviving


# --- admission face: proposal 087 chokepoint routing ----------------------


def _admission_stub(**overrides: Any) -> tuple[SimpleNamespace, MagicMock]:
    """A stub wired so ``route_feedback`` reaches the admission-merged guard
    with one changes-requested PR (lap budget disabled, ledger open).

    Returns ``(stub, model)`` so a test can assert whether the PR was admitted
    to ``model.pending_pr_requests``.
    """
    stub = _stub_agent(**overrides)
    model = MagicMock()
    model.pending_pr_requests = []
    model.pending_user_requests = []
    stub.consciousness.load_self_model.return_value = model
    stub.remediation_tracker.should_reroute.return_value = True
    stub._vcs.rev_parse.return_value = ""
    stub.memory.get_plan.return_value = None
    return stub, model


def _run_admission(stub: SimpleNamespace) -> None:
    """Drive ``route_feedback`` with one changes-requested PR (#10)."""
    _controller(stub).route_feedback(_changes_requested_result())


def test_route_feedback_admission_host_confirmed_landing_refuses() -> None:
    """A host-confirmed landing (``branch_landed``) refuses admission and
    short-circuits the ``origin/<branch>`` ancestry fallback entirely — the
    superset over a bare branch-ancestry check, which a squash/sibling merge
    would slip past while the PR object still reads OPEN."""
    stub, model = _admission_stub(
        is_pr_settled=MagicMock(
            return_value=_truth(
                "MERGED", settled=True, branch_landed=True, pr_number=10,
            ),
        ),
    )
    _run_admission(stub)
    assert model.pending_pr_requests == []  # refused
    stub._branch_merged_into_main.assert_not_called()


def test_route_feedback_admission_ancestry_fallback_refuses() -> None:
    """When the host does not confirm a landing (``branch_landed`` False) the
    guard falls back to the ``origin/<branch>`` ancestry probe — catching a
    cure that landed via squash/sibling/Tend while the PR object reads OPEN."""
    stub, model = _admission_stub()  # default OPEN observation
    stub._branch_merged_into_main = MagicMock(return_value=True)
    _run_admission(stub)
    assert model.pending_pr_requests == []
    stub._branch_merged_into_main.assert_called_once_with(
        "origin/maestro/evolve/plan-x",
    )


def test_route_feedback_admission_open_pr_admitted() -> None:
    """Neither the chokepoint (OPEN) nor the ancestry probe confirms a landing,
    so the PR is admitted to ``pending_pr_requests``."""
    stub, model = _admission_stub()  # default OPEN + ancestry False
    _run_admission(stub)
    assert len(model.pending_pr_requests) == 1
    assert "Remediate PR #10" in model.pending_pr_requests[0].directive


def test_route_feedback_prune_clears_resolved_via_chokepoint(
    mem_fs: dict[str, str],
) -> None:
    """The standdown prune routes ``_pr_resolved`` through
    ``is_pr_settled(.settled)`` — merged OR closed-unmerged both clear the
    standdown, and the legacy host oracle is never consulted (proposal 087)."""
    stub = _stub_agent()
    stub.config.pr_remediation_lap_budget = 3
    ledger = str(stub.config.memory_dir / "escalation_handoffs.json")
    mem_fs[ledger] = json.dumps({
        "50": {"head_sha": "h50", "delivered_at": "2026-01-01T00:00:00+00:00"},
        "60": {"head_sha": "h60", "delivered_at": "2026-01-01T00:00:00+00:00"},
    })

    def _settled(pr: int) -> MergeTruth:
        if pr == 50:  # merged → settled
            return _truth("MERGED", settled=True, branch_landed=True, pr_number=pr)
        if pr == 60:  # closed-unmerged → settled, not landed
            return _truth("CLOSED", settled=True, branch_landed=False, pr_number=pr)
        return _truth("OPEN", settled=False, branch_landed=False, pr_number=pr)

    stub.is_pr_settled = MagicMock(side_effect=_settled)

    _controller(stub).route_feedback(TendPRsResult())

    assert json.loads(mem_fs[ledger]) == {}
    stub._host_or_git_pr_merged.assert_not_called()


def test_route_feedback_repetition_skips_accrue_to_lap_budget_handoff(
    mem_fs: dict[str, str],
) -> None:
    """The #2727 / #2783 starvation shape: a PR the repetition gate keeps
    skipping — with **zero** success/partial stamps — accrues suppressed laps
    that reach the budget and trigger exactly one durable handoff, after which
    every later pass short-circuits on the standdown.

    Drives the real ``route_feedback`` against a real ``RemediationTracker`` and
    the real ``escalation_bridge`` (mem_fs only) so the skip-count accrual and
    the durable standdown are exercised end-to-end, not re-implemented.
    """
    from maestro import escalation_bridge
    from maestro.remediation_tracker import RemediationTracker

    mem = Path("/fake/mem")
    tracker = RemediationTracker(mem, threshold=2)
    # Latch the repetition gate: two consecutive no-ops → ``should_reroute``
    # returns False, so every subsequent pass is suppressed (no task, no stamp).
    tracker.record_outcome(10, noop=True)
    tracker.record_outcome(10, noop=True)

    stub = _stub_agent(remediation_tracker=tracker)
    stub.config.pr_remediation_lap_budget = 3
    model = MagicMock()
    model.pending_pr_requests = []
    model.pending_user_requests = []
    stub.consciousness.load_self_model.return_value = model
    stub._vcs.rev_parse.return_value = "headX"

    # Zero success/partial stamps: the only laps that land nothing are the
    # repetition-gate skips.
    with patch(
        "maestro.loop.pr_remediation.measure_unlanded_remediations",
        return_value={},
    ):
        # Three passes accrue skips (1, 2, 3); the fourth reaches the budget and
        # delivers; the fifth and sixth short-circuit on the standdown.
        for _ in range(6):
            _controller(stub).route_feedback(_changes_requested_result())

    # Exactly one durable handoff message + standdown for #10, despite six laps.
    raw = mem_fs.get(str(mem / "messages.jsonl"), "")
    msgs = [json.loads(line) for line in raw.splitlines() if line.strip()]
    assert len(msgs) == 1
    assert "#10" in msgs[0]["content"]
    assert escalation_bridge.has_standdown(JsonEscalationLedger(mem), 10) is True
    # The PR was never admitted to the remediation queue across all passes.
    assert model.pending_pr_requests == []

    # The standdown clears once the host reports the PR merged/closed, so a
    # genuinely new episode on a reopened PR can re-knock (not muted forever).
    cleared = escalation_bridge.prune_resolved_standdowns(JsonEscalationLedger(mem), lambda _pr: True)
    assert cleared == [10]
    assert escalation_bridge.has_standdown(JsonEscalationLedger(mem), 10) is False


def test_route_feedback_budget_disabled_records_no_skip() -> None:
    """With the budget disabled, a repetition-gate skip records no skip count —
    the lap-budget standdown stays fully disabled (#2783)."""
    stub = _stub_agent()  # pr_remediation_lap_budget defaults to 0
    model = MagicMock()
    model.pending_pr_requests = []
    model.pending_user_requests = []
    stub.consciousness.load_self_model.return_value = model
    stub.remediation_tracker.should_reroute.return_value = False
    stub._vcs.rev_parse.return_value = ""

    _controller(stub).route_feedback(_changes_requested_result())

    # The repetition gate skipped, but the budget is off → no skip recorded.
    stub.remediation_tracker.record_skip.assert_not_called()
    assert model.pending_pr_requests == []


def test_route_feedback_repetition_skip_records_when_budget_enabled() -> None:
    """When the budget is enabled, each repetition-gate skip records one skip
    toward the lap-budget standdown (#2783)."""
    stub = _stub_agent()
    stub.config.pr_remediation_lap_budget = 5
    model = MagicMock()
    model.pending_pr_requests = []
    model.pending_user_requests = []
    stub.consciousness.load_self_model.return_value = model
    stub.remediation_tracker.should_reroute.return_value = False
    # Below budget so no handoff fires this pass — isolate the skip recording.
    stub.remediation_tracker.skip_count.return_value = 0
    stub._vcs.rev_parse.return_value = ""

    with patch(
        "maestro.loop.pr_remediation.measure_unlanded_remediations",
        return_value={},
    ), patch(
        "maestro.loop.pr_remediation.escalation_bridge."
        "prune_resolved_standdowns",
    ), patch(
        "maestro.loop.pr_remediation.escalation_bridge.has_standdown",
        return_value=False,
    ), patch(
        "maestro.loop.pr_remediation.escalation_bridge.deliver_handoff",
    ) as deliver:
        _controller(stub).route_feedback(_changes_requested_result())

    stub.remediation_tracker.record_skip.assert_called_once_with(10)
    deliver.assert_not_called()
    assert model.pending_pr_requests == []


def _bug_comments_result(pr: int = 10) -> TendPRsResult:
    """A tend result carrying LLM-only review work (an unresolved AI nit)."""
    result = TendPRsResult()
    result.bug_comments_routed.append((
        pr, "title",
        [(1, "maestro/x.py", "fix the nit", "nit")],
        "plan-x", "pr body", f"https://gh/pr/{pr}", "maestro/evolve/x",
    ))
    return result


@pytest.mark.asyncio
async def test_merge_block_skip_does_not_exile_overflow_reroute(
    mem_fs: dict[str, str],
) -> None:
    """#2796: a PR poisoned only by merge-block skips still re-routes its later
    comment/overflow work.

    Two BEHIND_MAIN merge-block skips are recorded via the real
    ``gate_merge_block``; because they now land in the separate merge-block
    counter (not the goal-blind ``should_reroute`` outcome list), a subsequently
    detected AI-comment finding for the same PR is admitted to
    ``pending_pr_requests`` rather than dropped at the repetition gate.
    """
    from maestro.remediation_tracker import (
        MERGE_BLOCK_BEHIND_MAIN,
        RemediationTracker,
    )

    mem = Path("/fake/mem")
    tracker = RemediationTracker(mem, threshold=2)
    stub = _stub_agent(remediation_tracker=tracker)
    stub._probe_pr_merge_block = AsyncMock(
        return_value=(False, MERGE_BLOCK_BEHIND_MAIN),
    )
    controller = _controller(stub)
    goal = "Remediate PR #10: fix quality gates"

    assert await controller.gate_merge_block(
        goal, "maestro/evolve/x", "external",
    ) is True
    assert await controller.gate_merge_block(
        goal, "maestro/evolve/x", "external",
    ) is True

    # The deferrals went to the merge-block counter, NOT should_reroute's list.
    assert tracker.merge_block_skip_count(10) == 2
    assert tracker.should_reroute(10) is True
    # #2783 safety net: each skip still wrote a ``partial`` TaskRecord that the
    # unlanded sensor counts, so the lap-budget standdown stays reachable.
    recorded = [c.args[0] for c in stub.memory.record_task.call_args_list]
    assert len(recorded) == 2
    assert all(r.outcome == "partial" for r in recorded)

    # A later AI-comment finding for #10 must now be admitted — the repetition
    # gate no longer sees a poisoned no-op streak.
    model = MagicMock()
    model.pending_pr_requests = []
    model.pending_user_requests = []
    stub.consciousness.load_self_model.return_value = model
    stub._vcs.rev_parse.return_value = ""
    stub.memory.get_plan.return_value = None

    controller.route_feedback(_bug_comments_result(10))

    assert len(model.pending_pr_requests) == 1
    assert "Remediate PR #10" in model.pending_pr_requests[0].directive


def test_attempted_fruitless_comment_lap_still_blocks_reroute(
    mem_fs: dict[str, str],
) -> None:
    """#2744 guard intact: a genuinely attempted-but-fruitless comment lap
    (recorded the normal no-op way, no merge-block marker) still suppresses the
    re-route — only merge-block deferrals are spared the repetition gate.

    This is the adversarial contrast to
    ``test_merge_block_skip_does_not_exile_overflow_reroute``: recording the two
    no-ops the *pre-fix* way (plain ``record_outcome(noop=True)``, the path a
    merge-block skip used to take) re-poisons ``should_reroute`` and exiles the
    later AI-comment finding — proving the merge-block split is load-bearing.
    """
    from maestro.remediation_tracker import RemediationTracker

    mem = Path("/fake/mem")
    tracker = RemediationTracker(mem, threshold=2)
    # Two genuinely-attempted fruitless laps → should_reroute latches False.
    tracker.record_outcome(10, noop=True)
    tracker.record_outcome(10, noop=True)
    assert tracker.should_reroute(10) is False

    stub = _stub_agent(remediation_tracker=tracker)
    model = MagicMock()
    model.pending_pr_requests = []
    model.pending_user_requests = []
    stub.consciousness.load_self_model.return_value = model
    stub._vcs.rev_parse.return_value = ""
    stub.memory.get_plan.return_value = None

    _controller(stub).route_feedback(_bug_comments_result(10))

    # The attempted-fruitless streak still exiles the re-route.
    assert model.pending_pr_requests == []


# --- #2798 standdown carve-out for genuinely-new review threads --------------


def _seed_standdown(
    mem_fs: dict[str, str],
    pr: int,
    *,
    serviced: list[int] | None = None,
) -> None:
    """Seed an active standdown for *pr* under the stub's memory dir.

    The serviced set defaults to empty — the record-time shape ``deliver_handoff``
    writes (#2798), so a detected thread is treated as un-serviced and earns a
    lap.
    """
    ledger = str(Path("/fake/mem") / "escalation_handoffs.json")
    existing = json.loads(mem_fs[ledger]) if mem_fs.get(ledger) else {}
    existing[str(pr)] = {
        "head_sha": f"h{pr}",
        "delivered_at": "2026-01-01T00:00:00+00:00",
        "serviced_comment_ids": serviced if serviced is not None else [],
    }
    mem_fs[ledger] = json.dumps(existing)


def _stub_for_standdown_carveout() -> tuple[Any, Any]:
    """A stub agent + empty self-model wired for the carve-out route tests."""
    stub = _stub_agent()
    stub.config.pr_remediation_lap_budget = 3
    model = MagicMock()
    model.pending_pr_requests = []
    model.pending_user_requests = []
    stub.consciousness.load_self_model.return_value = model
    stub._vcs.rev_parse.return_value = ""
    stub.memory.get_plan.return_value = None
    return stub, model


def test_route_feedback_standdown_routes_unserviced_review_thread(
    mem_fs: dict[str, str],
) -> None:
    """#2798 AC1/AC3: a stood-down PR carrying a genuinely-new review thread is
    admitted exactly once; admission no longer stamps it serviced (proposal
    091 — that moved to reply-delivery), so it stays un-serviced for now."""
    from maestro import escalation_bridge

    mem = Path("/fake/mem")
    _seed_standdown(mem_fs, 10)
    stub, model = _stub_for_standdown_carveout()

    with patch(
        "maestro.loop.pr_remediation.measure_unlanded_remediations",
        return_value={},
    ):
        _controller(stub).route_feedback(_bug_comments_result(10))

    assert len(model.pending_pr_requests) == 1
    assert "Remediate PR #10" in model.pending_pr_requests[0].directive
    stub.consciousness.mutate_self_model.assert_called_once()
    # The standdown persists; admission no longer stamps the routed thread
    # serviced (proposal 091), so comment-id 1 stays un-serviced until its
    # terminal reply is confirmed delivered.
    assert escalation_bridge.has_standdown(JsonEscalationLedger(mem), 10) is True
    assert escalation_bridge.unserviced_comment_ids(JsonEscalationLedger(mem), 10, {1}) == {1}


def test_route_feedback_reconcile_leaves_answered_serviced_id(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 091: reconciliation drops only ids still shown unanswered this
    pass. A genuinely-answered thread carries a terminal marker and is excluded
    from ``pass_comment_ids`` (here comment-id 99), so its serviced stamp is
    left intact — while a genuinely-new thread (comment-id 1) is still admitted.
    This is the treadmill guard's post-091 shape: answered threads are held by
    the terminal-marker exclusion, not by an admission-time serviced stamp."""
    from maestro import escalation_bridge

    mem = Path("/fake/mem")
    # 99 is an already-answered thread's id (serviced, absent from this pass);
    # comment-id 1 is the genuinely-new thread the tend result carries.
    _seed_standdown(mem_fs, 10, serviced=[99])
    stub, model = _stub_for_standdown_carveout()

    with patch(
        "maestro.loop.pr_remediation.measure_unlanded_remediations",
        return_value={},
    ):
        _controller(stub).route_feedback(_bug_comments_result(10))

    # The new thread is admitted for one lap...
    assert len(model.pending_pr_requests) == 1
    # ...and the answered thread's serviced stamp survives (never in this pass,
    # so reconciliation leaves it untouched).
    assert escalation_bridge.unserviced_comment_ids(JsonEscalationLedger(mem), 10, {99}) == set()


def test_route_feedback_undelivered_thread_reroutes_next_pass(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 091: a force-admitted thread whose reply never delivered is NOT
    stamped serviced at admission, so it stays un-serviced and earns another lap
    on the next pass — a failed delivery is retried, not buried (the bug the old
    admission-time stamp caused)."""
    _seed_standdown(mem_fs, 10)
    stub, model = _stub_for_standdown_carveout()
    controller = _controller(stub)

    with patch(
        "maestro.loop.pr_remediation.measure_unlanded_remediations",
        return_value={},
    ):
        controller.route_feedback(_bug_comments_result(10))
    assert len(model.pending_pr_requests) == 1

    # No delivery stamp fires (the reply never posted). Drain the queue and route
    # again: the thread is still un-serviced, so it is re-admitted for a retry.
    model.pending_pr_requests = []
    with patch(
        "maestro.loop.pr_remediation.measure_unlanded_remediations",
        return_value={},
    ):
        controller.route_feedback(_bug_comments_result(10))

    assert len(model.pending_pr_requests) == 1


def test_route_feedback_reconciles_falsely_serviced_thread(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 091 self-heal: a legacy standdown that blanket-stamped comment-id
    1 serviced (before its reply ever delivered) is repaired at tend time —
    ``reconcile_serviced_comment_ids`` drops the falsely-serviced id because the
    thread still shows unanswered, so the carve-out re-admits it for a reply lap
    instead of muting it forever."""
    from maestro import escalation_bridge

    mem = Path("/fake/mem")
    # Falsely-serviced: comment-id 1 is stamped serviced but its thread is still
    # unanswered (the pre-091 admission-stamp corruption this repairs).
    _seed_standdown(mem_fs, 10, serviced=[1])
    stub, model = _stub_for_standdown_carveout()

    with patch(
        "maestro.loop.pr_remediation.measure_unlanded_remediations",
        return_value={},
    ):
        _controller(stub).route_feedback(_bug_comments_result(10))

    # Without reconciliation the falsely-serviced id would suppress the lap
    # (0 admitted); reconciliation drops it, reopening the valve for one lap.
    assert len(model.pending_pr_requests) == 1
    assert "Remediate PR #10" in model.pending_pr_requests[0].directive
    # The dropped id, with admission no longer stamping, stays un-serviced — the
    # legacy set self-healed and the thread is eligible until its reply lands.
    assert escalation_bridge.unserviced_comment_ids(JsonEscalationLedger(mem), 10, {1}) == {1}


def test_route_feedback_standdown_no_comment_problem_stays_suppressed(
    mem_fs: dict[str, str],
) -> None:
    """#2798 AC2: a stood-down PR whose only problem carries no review-thread
    comment-id (here a CHANGES_REQUESTED route) stays fully suppressed."""
    _seed_standdown(mem_fs, 10)
    stub, model = _stub_for_standdown_carveout()
    stub.remediation_tracker.should_reroute.return_value = True

    with patch(
        "maestro.loop.pr_remediation.measure_unlanded_remediations",
        return_value={},
    ):
        _controller(stub).route_feedback(_changes_requested_result(10))

    assert model.pending_pr_requests == []
    stub.consciousness.mutate_self_model.assert_not_called()


def test_route_feedback_standdown_routes_thread_predating_record(
    mem_fs: dict[str, str],
) -> None:
    """#2798 constraint 1 (the PR #2737 shape): a thread that predates the
    standdown record is still routed once, because the serviced set is seeded
    empty at record time — never keyed on 'new since the standdown'."""
    from maestro import escalation_bridge

    mem = Path("/fake/mem")
    # Record the standdown via the real bridge: it seeds serviced_comment_ids=[],
    # so a thread that already existed when it was recorded is still un-serviced.
    handoff = escalation_bridge.UnlandedCureHandoff(
        pr_number=10, head_sha="h10", stamp_count=9,
    )
    assert escalation_bridge.deliver_handoff(
        mem, handoff, backend=JsonEscalationLedger(mem),
        now="2026-01-01T00:00:00+00:00",
        claim=StanddownClaim.HANDOFF_READY,
    )
    stub, model = _stub_for_standdown_carveout()

    with patch(
        "maestro.loop.pr_remediation.measure_unlanded_remediations",
        return_value={},
    ):
        _controller(stub).route_feedback(_bug_comments_result(10))

    assert len(model.pending_pr_requests) == 1
    # Admission no longer stamps (proposal 091): the predating thread is routed
    # once and stays un-serviced until its reply is confirmed delivered.
    assert escalation_bridge.unserviced_comment_ids(JsonEscalationLedger(mem), 10, {1}) == {1}


def test_route_feedback_force_admit_bypasses_downstream_gates(
    mem_fs: dict[str, str],
) -> None:
    """#2798 constraint 2/AC3: under the carve-out the PR-ledger, repetition, and
    preflight-pass SHA gates are skipped, so a new thread reaches the enqueue
    instead of dying one gate later — even with all three set to suppress."""
    _seed_standdown(mem_fs, 10)
    pr_ledger_mock = MagicMock()
    pr_ledger_mock.can_attempt.return_value = False
    stub, model = _stub_for_standdown_carveout()
    stub.pr_ledger = pr_ledger_mock
    stub.remediation_tracker.should_reroute.return_value = False

    # The preflight-pass SHA gate now reads through the backend seam; set it to
    # suppress so the assertion proves the carve-out returns *before* consulting
    # it (not merely that it happened to answer "don't skip").
    stub.preflight_passes.should_skip.return_value = True

    with patch(
        "maestro.loop.pr_remediation.measure_unlanded_remediations",
        return_value={},
    ):
        _controller(stub).route_feedback(_bug_comments_result(10))

    # Admitted despite every downstream gate set to suppress.
    assert len(model.pending_pr_requests) == 1
    # None of the three GitHub-only-signal gates were consulted.
    pr_ledger_mock.can_attempt.assert_not_called()
    stub.remediation_tracker.should_reroute.assert_not_called()
    stub.remediation_tracker.record_skip.assert_not_called()
    stub.preflight_passes.should_skip.assert_not_called()


# ── Thread-reply manifest reader (#2817) ──────────────────────────────


def _manifest_path(artifacts: Path) -> str:
    return str(artifacts / _REPLY_MANIFEST_FILENAME)


def test_read_thread_reply_manifest_parses_well_formed(
    mem_fs: dict[str, str],
) -> None:
    """A well-formed JSONL manifest yields one ``(comment_id, body)`` per line."""
    artifacts = Path("/fake/artifacts/task1")
    mem_fs[_manifest_path(artifacts)] = (
        json.dumps({"comment_id": 100, "body": "Fixed locally with x."}) + "\n"
        + json.dumps({"comment_id": 200, "body": "Tracked in #5"}) + "\n"
    )

    assert read_thread_reply_manifest(artifacts) == [
        (100, "Fixed locally with x."),
        (200, "Tracked in #5"),
    ]


def test_read_thread_reply_manifest_missing_file_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """A missing manifest is not an error — there is simply nothing to deliver."""
    assert read_thread_reply_manifest(Path("/fake/artifacts/none")) == []


def test_read_thread_reply_manifest_empty_and_blank_lines(
    mem_fs: dict[str, str],
) -> None:
    """An empty manifest, and blank lines within one, yield no replies."""
    artifacts = Path("/fake/artifacts/task2")
    mem_fs[_manifest_path(artifacts)] = "\n   \n\n"

    assert read_thread_reply_manifest(artifacts) == []


def test_read_thread_reply_manifest_dedupes_by_comment_id(
    mem_fs: dict[str, str],
) -> None:
    """Duplicate lines for one ``comment_id`` collapse to one reply (last wins).

    The append-only manifest accumulates across plan steps, and a
    ``Planner.replan`` re-run of a recording step can append a second line for
    the same thread. Returning both would make the loop double-post the thread
    — the exact outcome the loop-as-sole-deliverer design prevents — so the
    reader keeps one reply per ``comment_id`` (last write wins) while preserving
    first-seen order across distinct ids.
    """
    artifacts = Path("/fake/artifacts/task4")
    mem_fs[_manifest_path(artifacts)] = "\n".join([
        json.dumps({"comment_id": 100, "body": "first body"}),
        json.dumps({"comment_id": 200, "body": "other thread"}),
        json.dumps({"comment_id": 100, "body": "replan re-record"}),  # dup id
    ])

    assert read_thread_reply_manifest(artifacts) == [
        (100, "replan re-record"),
        (200, "other thread"),
    ]


def test_read_thread_reply_manifest_skips_malformed_lines(
    mem_fs: dict[str, str],
) -> None:
    """A malformed line is logged and skipped; well-formed lines still parse.

    Covers every reject branch: non-JSON, non-object JSON, missing/typed-wrong
    ``comment_id``, missing ``body``, and an empty ``body``.
    """
    artifacts = Path("/fake/artifacts/task3")
    mem_fs[_manifest_path(artifacts)] = "\n".join([
        "{not json",                                          # JSONDecodeError
        json.dumps([1, 2, 3]),                               # not a dict
        json.dumps({"comment_id": "x", "body": "non-int id"}),  # id not int
        json.dumps({"body": "no comment_id"}),               # missing id
        json.dumps({"comment_id": 7}),                       # missing body
        json.dumps({"comment_id": 8, "body": ""}),           # empty body
        json.dumps({"comment_id": 9, "body": "Good reply."}),  # the one keeper
    ])

    assert read_thread_reply_manifest(artifacts) == [(9, "Good reply.")]


# --- #2893 reply-completion for outdated, un-replied review threads -----------


def _reply_completion_result(pr: int = 10) -> TendPRsResult:
    """A tend result carrying an outdated, un-replied thread needing a reply."""
    result = TendPRsResult()
    result.reply_completion_routed.append((
        pr, "title",
        [(1, "maestro/x.py", "stale finding", "bug")],
        "plan-x", "pr body", f"https://gh/pr/{pr}", "maestro/evolve/x",
    ))
    return result


def test_format_reply_completion_problem_covers_icon_and_human() -> None:
    """The reply-completion problem lists each thread (an icon for AI findings,
    no icon for human threads) and carries the marker the preflight/merge-block
    gates key on so the reply lap is never Tend-skipped (#2893)."""
    from maestro.loop.pr_remediation import (
        _COMMENT_REVIEW_PROBLEM_MARKERS,
        _REPLY_COMPLETION_PROBLEM_MARKER,
        _format_reply_completion_problem,
    )

    text = _format_reply_completion_problem(
        7,
        [
            (1, "a.py", "Off-by-one", "bug"),
            (2, "b.py", "Please clarify", "human"),
        ],
    )
    assert _REPLY_COMPLETION_PROBLEM_MARKER in text
    assert "🐛 Off-by-one (a.py, comment 1)" in text
    # Human threads carry no icon prefix.
    assert "Please clarify (b.py, comment 2)" in text
    assert "🐛 Please clarify" not in text
    # The marker is in the shared review set, so the merge-block and preflight
    # gates treat a reply-completion directive as LLM comment-review work.
    assert _REPLY_COMPLETION_PROBLEM_MARKER in _COMMENT_REVIEW_PROBLEM_MARKERS


def test_reply_completion_routes_outdated_thread_under_standdown(
    mem_fs: dict[str, str],
) -> None:
    """#2893: a stood-down PR whose only outstanding thread is outdated +
    un-replied is routed exactly one reply-completion lap (with a
    reply-completion problem block) and its comment id is marked serviced — so
    the thread is not frozen."""
    from maestro import escalation_bridge

    mem = Path("/fake/mem")
    _seed_standdown(mem_fs, 10)
    stub, model = _stub_for_standdown_carveout()

    with patch(
        "maestro.loop.pr_remediation.measure_unlanded_remediations",
        return_value={},
    ):
        _controller(stub).route_feedback(_reply_completion_result(10))

    assert len(model.pending_pr_requests) == 1
    directive = model.pending_pr_requests[0].directive
    assert "Remediate PR #10" in directive
    assert "outdated review thread(s) needing a terminal reply marker" in directive
    stub.consciousness.mutate_self_model.assert_called_once()
    # The standdown persists; admission no longer stamps (proposal 091), so the
    # reply-completion thread stays un-serviced until its terminal reply lands.
    assert escalation_bridge.has_standdown(JsonEscalationLedger(mem), 10) is True
    assert escalation_bridge.unserviced_comment_ids(JsonEscalationLedger(mem), 10, {1}) == {1}


def test_reply_completion_skipped_without_standdown(
    mem_fs: dict[str, str],
) -> None:
    """#2893: an outdated-only PR with NO standdown is not added to
    ``aggregated`` — today's behavior preserved, the treadmill not reopened."""
    stub, model = _stub_for_standdown_carveout()
    # No standdown seeded → ``has_standdown`` is False for PR 10.

    with patch(
        "maestro.loop.pr_remediation.measure_unlanded_remediations",
        return_value={},
    ):
        _controller(stub).route_feedback(_reply_completion_result(10))

    assert model.pending_pr_requests == []
    stub.consciousness.mutate_self_model.assert_not_called()


def test_reply_completion_skipped_when_budget_disabled(
    mem_fs: dict[str, str],
) -> None:
    """#2893: with the lap budget disabled (<= 0) the standdown machinery is
    off, so reply-completion never surfaces — the PR is not added to
    ``aggregated`` regardless of any standdown state."""
    stub = _stub_agent()  # ``pr_remediation_lap_budget`` defaults to 0
    model = MagicMock()
    model.pending_pr_requests = []
    model.pending_user_requests = []
    stub.consciousness.load_self_model.return_value = model

    with patch(
        "maestro.loop.pr_remediation.measure_unlanded_remediations",
        return_value={},
    ):
        _controller(stub).route_feedback(_reply_completion_result(10))

    assert model.pending_pr_requests == []
    stub.consciousness.mutate_self_model.assert_not_called()


# --- route_user_pr_message: human-demand routing (proposal 091) -------------


def _pr_state(number: int, head_ref: str) -> PrMergeState:
    """A minimal open ``PrMergeState`` for the human-demand routing tests."""
    return PrMergeState(
        number=number,
        head_ref=head_ref,
        title=f"PR {number}",
        body="body text",
        state="OPEN",
        mergeable="MERGEABLE",
        merge_state_status="CLEAN",
        review_decision="",
        created_at="2026-06-16T00:00:00Z",
        updated_at="2026-06-16T00:00:00Z",
        url=f"https://github.com/o/r/pull/{number}",
    )


def _seed_suppressions(
    memory_dir: Path, mem_fs: dict[str, str], pr: int, head_sha: str,
) -> None:
    """Seed both suppression stores for *pr* so a release is observable."""
    record_stand_down(
        pr, head_sha, JsonlStandDownVerdictLedger(memory_dir),
        now="2026-06-16T00:00:00+00:00",
    )
    mem_fs[str(memory_dir / "escalation_handoffs.json")] = json.dumps({
        str(pr): {
            "head_sha": head_sha,
            "delivered_at": "2026-06-16T00:00:00+00:00",
            "serviced_comment_ids": [],
        },
    })


def test_route_user_pr_message_open_pr_yields_directive_and_clears_stores(
    mem_fs: dict[str, str],
) -> None:
    """A human message naming an open own PR yields a parseable remediation
    directive and releases both suppression stores before returning."""
    stub = _stub_agent()
    stub.evolution.code_host.get_open_evolution_pr_states = MagicMock(
        return_value={
            "maestro/evolve/ep42": _pr_state(2884, "maestro/evolve/ep42"),
        },
    )
    stub.memory.get_plan.return_value = None
    memory_dir = stub.config.memory_dir
    _seed_suppressions(memory_dir, mem_fs, 2884, "sha1")
    assert find_stand_down(
        2884, "sha1", JsonlStandDownVerdictLedger(memory_dir),
    ) is not None
    assert has_standdown(JsonEscalationLedger(memory_dir), 2884) is True

    result = _controller(stub).route_user_pr_message(
        "PR #2884 still has unresolved comments",
    )

    assert result is not None
    directive, head_ref = result
    assert head_ref == "maestro/evolve/ep42"
    assert directive.splitlines()[0] == "Remediate PR #2884: PR 2884"
    assert "still has unresolved comments" in directive
    # Both suppression stores released before the run.
    assert find_stand_down(
        2884, "sha1", JsonlStandDownVerdictLedger(memory_dir),
    ) is None
    assert has_standdown(JsonEscalationLedger(memory_dir), 2884) is False
    stub.evolution.code_host.get_open_evolution_pr_states.\
        assert_called_once_with("maestro/evolve/")
    stub.memory.get_plan.assert_called_once_with("ep42")


def test_route_user_pr_message_plan_block_names_no_stage(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 149: the human-demand directive's plan block names no stage."""
    stub = _stub_agent()
    stub.evolution.code_host.get_open_evolution_pr_states = MagicMock(
        return_value={
            "maestro/evolve/ep42": _pr_state(2884, "maestro/evolve/ep42"),
        },
    )
    stub.memory.get_plan.return_value = EvolutionPlan(
        plan_id="ep42", title="t", category="capability",
        rationale="r", proposed_changes=[], files_affected=[],
        expected_impact="i", priority="capability",
    )

    result = _controller(stub).route_user_pr_message("PR #2884 please fix")

    assert result is not None
    directive, _head_ref = result
    assert "Stage:" not in directive
    assert "- Category: capability / Priority: capability" in directive


def test_route_user_pr_message_open_pr_without_verdict_writes_no_release_row(
    mem_fs: dict[str, str],
) -> None:
    """A human demand on a never-stood-down PR appends no release row, so the
    live PR is not banked as disposed (#4314)."""
    stub = _stub_agent()
    stub.evolution.code_host.get_open_evolution_pr_states = MagicMock(
        return_value={
            "maestro/evolve/ep42": _pr_state(2884, "maestro/evolve/ep42"),
        },
    )
    stub.memory.get_plan.return_value = None
    memory_dir = stub.config.memory_dir

    result = _controller(stub).route_user_pr_message("PR #2884 needs a look")

    assert result is not None
    assert result[1] == "maestro/evolve/ep42"
    assert JsonlStandDownVerdictLedger(memory_dir).banked_pr_ids() == set()


def test_route_user_pr_message_foreign_pr_returns_none_and_keeps_stores(
    mem_fs: dict[str, str],
) -> None:
    """A message naming a PR that is not an open own PR returns ``None`` and
    leaves both suppression stores untouched."""
    stub = _stub_agent()
    # #2884 is named but only #2900 is an open own PR.
    stub.evolution.code_host.get_open_evolution_pr_states = MagicMock(
        return_value={
            "maestro/evolve/ep99": _pr_state(2900, "maestro/evolve/ep99"),
        },
    )
    stub.memory.get_plan.return_value = None
    memory_dir = stub.config.memory_dir
    _seed_suppressions(memory_dir, mem_fs, 2884, "sha1")

    result = _controller(stub).route_user_pr_message("PR #2884 is not ready")

    assert result is None
    assert find_stand_down(
        2884, "sha1", JsonlStandDownVerdictLedger(memory_dir),
    ) is not None
    assert has_standdown(JsonEscalationLedger(memory_dir), 2884) is True


def test_route_user_pr_message_no_pr_reference_returns_none(
    mem_fs: dict[str, str],
) -> None:
    """A message naming no PR returns ``None`` without consulting the host."""
    stub = _stub_agent()
    stub.evolution.code_host.get_open_evolution_pr_states = MagicMock(
        return_value={},
    )

    result = _controller(stub).route_user_pr_message(
        "please improve the journal formatting",
    )

    assert result is None
    stub.evolution.code_host.get_open_evolution_pr_states.assert_not_called()


# ---------------------------------------------------------------------------
# Refutation upkeep — the third standdown release path (proposal 135)
# ---------------------------------------------------------------------------


def _handoff_ledger(mem_fs: dict[str, str], memory_dir: Path, pr: int) -> str:
    """Seed a ``handoff-ready``-claimed standdown for *pr*; return its path."""
    path = str(memory_dir / "escalation_handoffs.json")
    mem_fs[path] = json.dumps({
        str(pr): {
            "head_sha": "abc12345",
            "delivered_at": "2026-01-01T00:00:00+00:00",
            "serviced_comment_ids": [],
            "claim": "handoff-ready",
        },
    })
    return path


def test_route_feedback_refutation_prune_not_called_with_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Flag off: the upkeep block is the unchanged two-pruner path.

    The refuting reading is present on the result, so only the flag keeps the
    release from firing — today's behaviour stays byte-identical until it is
    deliberately switched on.
    """
    stub = _stub_agent()
    stub.config.pr_remediation_lap_budget = 6
    path = _handoff_ledger(mem_fs, stub.config.memory_dir, 10)

    with patch(
        "maestro.loop.pr_remediation.escalation_bridge."
        "prune_refuted_standdowns",
    ) as refute_prune:
        _controller(stub).route_feedback(
            TendPRsResult(
            owed_acts_observed={10: "code-owed"},
            owned_blockers_observed={10: "CONFLICTING"},
        ),
        )

    refute_prune.assert_not_called()
    stub.gate_activations.append.assert_not_called()
    assert "10" in json.loads(mem_fs[path])


def test_route_feedback_refutation_releases_latch_and_logs_one_activation(
    mem_fs: dict[str, str],
) -> None:
    """Flag on: a refuted ``handoff-ready`` latch is released, and exactly one
    ``standdown_refuted`` gate-activation row records the release."""
    stub = _stub_agent()
    stub.config.pr_remediation_lap_budget = 6
    stub.config.refutable_standdown_enabled = True
    path = _handoff_ledger(mem_fs, stub.config.memory_dir, 10)

    _controller(stub).route_feedback(
        TendPRsResult(
            owed_acts_observed={10: "code-owed"},
            owned_blockers_observed={10: "CONFLICTING"},
        ),
    )

    assert json.loads(mem_fs[path]) == {}
    assert has_standdown(stub.escalation_standdowns, 10) is False
    assert stub.gate_activations.append.call_count == 1
    record = stub.gate_activations.append.call_args.args[0]
    assert record.gate == "standdown_refuted"
    assert record.action == "released"
    assert "#10" in record.detail
    assert "code-owed" in record.detail


def test_route_feedback_refutation_holds_on_a_non_refuting_reading(
    mem_fs: dict[str, str],
) -> None:
    """Flag on but the live reading still supports the claim — the latch holds
    and nothing is stamped."""
    stub = _stub_agent()
    stub.config.pr_remediation_lap_budget = 6
    stub.config.refutable_standdown_enabled = True
    path = _handoff_ledger(mem_fs, stub.config.memory_dir, 10)

    _controller(stub).route_feedback(
        TendPRsResult(owed_acts_observed={10: "wait-ci"}),
    )

    assert "10" in json.loads(mem_fs[path])
    stub.gate_activations.append.assert_not_called()


def test_route_feedback_refutation_holds_an_unmeasured_pr(
    mem_fs: dict[str, str],
) -> None:
    """A PR the tend walk never measured maps to ``None`` — unmeasured is not
    refuted, so the latch survives this pass rather than being released on an
    absent reading."""
    stub = _stub_agent()
    stub.config.pr_remediation_lap_budget = 6
    stub.config.refutable_standdown_enabled = True
    path = _handoff_ledger(mem_fs, stub.config.memory_dir, 10)

    _controller(stub).route_feedback(TendPRsResult(owed_acts_observed={}))

    assert "10" in json.loads(mem_fs[path])
    stub.gate_activations.append.assert_not_called()


def test_route_feedback_refutation_skipped_when_lap_budget_disabled(
    mem_fs: dict[str, str],
) -> None:
    """Under the same ``lap_budget > 0`` guard as the merge/close pruner: both
    release *PR handoff* latches, whose only writer is itself lap-budget-gated,
    so a budget-0 deployment has none to release."""
    stub = _stub_agent()  # pr_remediation_lap_budget defaults to 0
    stub.config.refutable_standdown_enabled = True
    path = _handoff_ledger(mem_fs, stub.config.memory_dir, 10)

    with patch(
        "maestro.loop.pr_remediation.escalation_bridge."
        "prune_refuted_standdowns",
    ) as refute_prune:
        _controller(stub).route_feedback(
            TendPRsResult(
            owed_acts_observed={10: "code-owed"},
            owned_blockers_observed={10: "CONFLICTING"},
        ),
        )

    refute_prune.assert_not_called()
    assert "10" in json.loads(mem_fs[path])


def test_route_feedback_refutation_adds_no_host_call(
    mem_fs: dict[str, str],
) -> None:
    """The release costs nothing: the flag-on upkeep makes exactly the code-host
    calls the flag-off upkeep makes. The owed act comes entirely off
    ``TendPRsResult``, so no host round-trip can be added by switching it on."""

    def _host_calls(*, refutable: bool) -> list[Any]:
        stub = _stub_agent()
        stub.config.pr_remediation_lap_budget = 6
        stub.config.refutable_standdown_enabled = refutable
        host = MagicMock()
        host.is_pr_closed.return_value = False
        stub.evolution = SimpleNamespace(code_host=host)
        _handoff_ledger(mem_fs, stub.config.memory_dir, 10)
        _controller(stub).route_feedback(
            TendPRsResult(
            owed_acts_observed={10: "code-owed"},
            owned_blockers_observed={10: "CONFLICTING"},
        ),
        )
        return host.mock_calls

    off = _host_calls(refutable=False)
    on = _host_calls(refutable=True)
    assert on == off


def _sent_messages(mem_fs: dict[str, str], memory_dir: Path) -> list[str]:
    """Return the message ids appended to *memory_dir*'s ``messages.jsonl``."""
    raw = mem_fs.get(str(memory_dir / "messages.jsonl"), "")
    return [
        json.loads(line)["message_id"]
        for line in raw.splitlines()
        if line.strip()
    ]


def _refuted_result() -> TendPRsResult:
    """The measured refuting reading the tend walk carries out for PR 10."""
    return TendPRsResult(
        owed_acts_observed={10: "code-owed"},
        owned_blockers_observed={10: "CONFLICTING"},
    )


def test_route_feedback_refutation_withdraws_its_own_message(
    mem_fs: dict[str, str],
) -> None:
    """The release retracts the stand-down it is undoing, naming the blocker.

    The handoff told Thomas only he could land PR #10; the release measured that
    false, so it must not silently delete the ledger row and leave the claim
    standing in his inbox.
    """
    stub = _stub_agent()
    stub.config.pr_remediation_lap_budget = 6
    stub.config.refutable_standdown_enabled = True
    _handoff_ledger(mem_fs, stub.config.memory_dir, 10)

    _controller(stub).route_feedback(_refuted_result())

    assert _sent_messages(mem_fs, stub.config.memory_dir) == [
        "standdown-withdrawn-10-1",
    ]
    raw = mem_fs[str(stub.config.memory_dir / "messages.jsonl")]
    assert "CONFLICTING" in raw


def test_route_feedback_refutation_cap_stamps_a_distinct_activation(
    mem_fs: dict[str, str],
) -> None:
    """Past the release budget the latch is permanent, and ``gate_activations``
    distinguishes that terminal from an ordinary release."""
    stub = _stub_agent()
    stub.config.pr_remediation_lap_budget = 6
    stub.config.refutable_standdown_enabled = True
    controller = _controller(stub)
    for _ in range(stub.config.standdown_refutation_release_cap):
        _handoff_ledger(mem_fs, stub.config.memory_dir, 10)
        controller.route_feedback(_refuted_result())
    path = _handoff_ledger(mem_fs, stub.config.memory_dir, 10)

    stub.gate_activations.append.reset_mock()
    controller.route_feedback(_refuted_result())

    assert "10" in json.loads(mem_fs[path])
    assert stub.gate_activations.append.call_count == 1
    record = stub.gate_activations.append.call_args.args[0]
    assert record.gate == "standdown_refuted"
    assert record.action == "capped"
    assert "#10" in record.detail
    assert "standdown-refutation-capped-10" in _sent_messages(
        mem_fs, stub.config.memory_dir,
    )

    # A later pass over the capped PR is a complete no-op — no second terminal
    # message, no second activation row.
    stub.gate_activations.append.reset_mock()
    controller.route_feedback(_refuted_result())
    stub.gate_activations.append.assert_not_called()
    assert _sent_messages(mem_fs, stub.config.memory_dir).count(
        "standdown-refutation-capped-10",
    ) == 1


def test_route_feedback_lap_budget_standdown_is_not_refuted_by_code_owed(
    mem_fs: dict[str, str],
) -> None:
    """An exhaustion latch holds under a ``code-owed`` reading (#4110).

    The lap-budget arm hands up a PR it spent its laps on — typically still
    conflicting, so the live act is ``code-owed``. Stamping that record
    ``handoff-ready`` let the refutation prune release it and the same pass
    re-deliver it, looping withdrawal/handoff messages until the release cap was
    spent. Stamped ``lap-budget-exhausted``, the latch holds across passes: one
    handoff message, no withdrawal, no capped terminal.
    """
    stub = _stub_agent()
    stub.config.pr_remediation_lap_budget = 2
    stub.config.refutable_standdown_enabled = True
    model = MagicMock()
    model.pending_pr_requests = []
    model.pending_user_requests = []
    stub.consciousness.load_self_model.return_value = model
    stub.remediation_tracker.should_reroute.return_value = True
    stub._vcs.rev_parse.return_value = "head1"
    controller = _controller(stub)

    with patch(
        "maestro.loop.pr_remediation.measure_unlanded_remediations",
        return_value={10: 9},
    ):
        for _ in range(3):
            result = _changes_requested_result()
            result.owed_acts_observed = {10: "code-owed"}
            result.owned_blockers_observed = {10: "CONFLICTING"}
            controller.route_feedback(result)

    sent = _sent_messages(mem_fs, stub.config.memory_dir)
    assert len(sent) == 1
    assert sent[0].startswith("handoff-10-")
    ledger = json.loads(
        mem_fs[str(stub.config.memory_dir / "escalation_handoffs.json")],
    )
    assert ledger["10"]["claim"] == "lap-budget-exhausted"


def test_route_feedback_refutation_makes_the_pr_remediation_eligible_again(
    mem_fs: dict[str, str],
) -> None:
    """End-to-end pin for the regression this path exists for.

    A green PR was handed off and latched; a sibling merge then turned it
    ``CONFLICTING``, so the tend walk measures ``code-owed``. One pass releases
    the latch, and ``has_standdown`` — the per-number predicate the Tend route's
    minting gate consults — flips to ``False``, so the ordinary conflict route
    can mint the remediation it always would have.
    """
    stub = _stub_agent()
    stub.config.pr_remediation_lap_budget = 6
    stub.config.refutable_standdown_enabled = True
    _handoff_ledger(mem_fs, stub.config.memory_dir, 10)
    # The minting gate is suppressed while the latch stands.
    assert has_standdown(stub.escalation_standdowns, 10) is True

    _controller(stub).route_feedback(
        TendPRsResult(
            owed_acts_observed={10: "code-owed"},
            owned_blockers_observed={10: "CONFLICTING"},
        ),
    )

    assert has_standdown(stub.escalation_standdowns, 10) is False


def test_route_feedback_dedups_against_live_queue_not_snapshot() -> None:
    """The transactional append re-runs the header dedup on the live model.

    A writer that enqueued the same PR during this pass's ``gh``/git window is
    invisible to the snapshot ``_pr_already_pending`` closed over; appending
    blindly would duplicate the entry — the dual of the lost update #3716 fixed.
    """
    stub = _stub_agent()
    stub.config.pr_remediation_lap_budget = 5
    snapshot = MagicMock()
    snapshot.pending_pr_requests = []
    snapshot.pending_user_requests = []
    stub.consciousness.load_self_model.return_value = snapshot
    stub.remediation_tracker.should_reroute.return_value = True
    stub._vcs.rev_parse.return_value = ""
    stub.memory.get_plan.return_value = None

    # The live model already carries a rival admission for the same PR.
    rival = PendingPRRequest(directive="Remediate PR #10: rival", pr_branch="b")
    live = MagicMock()
    live.pending_pr_requests = [rival]
    stub.consciousness.mutate_self_model.side_effect = lambda fn: fn(live)

    with patch(
        "maestro.loop.pr_remediation.measure_unlanded_remediations",
        return_value={10: 2},
    ), patch(
        "maestro.loop.pr_remediation.escalation_bridge."
        "prune_resolved_standdowns",
    ), patch(
        "maestro.loop.pr_remediation.escalation_bridge.has_standdown",
        return_value=False,
    ), patch(
        "maestro.loop.pr_remediation.escalation_bridge.deliver_handoff",
    ):
        _controller(stub).route_feedback(_changes_requested_result())

    # No duplicate: the live queue still holds exactly the rival entry.
    assert live.pending_pr_requests == [rival]


@pytest.mark.asyncio
async def test_every_evicting_arm_spares_reply_completion_lap() -> None:
    """No zero-cost evicting dequeue arm fires on a reply-completion goal.

    The ready, stand-down and merge-block arms are wired to their real bodies;
    the ready and merge-block observations are stubbed to "would evict" and the
    stand-down arm must return before its head probe. The evicting set is the
    table minus an explicit allowlist of arms that never evict a live lap, so a
    new table state lands here by default (#2968, #4122).
    """

    stub = _stub_agent()
    stub.config.observed_ready_standdown_enabled = True
    stub.observe_pr_readiness = MagicMock(
        return_value=SimpleNamespace(ready_and_waiting=True),
    )
    stub._probe_pr_merge_block = AsyncMock(
        return_value=(False, MERGE_BLOCK_BEHIND_MAIN),
    )
    stub._vcs = MagicMock()
    ctrl = _controller(stub)
    stub._gate_pr_remediation_ready = (
        lambda entry, source: PrRemediationController(stub)._gate_pr_remediation_ready(entry, source,  # type: ignore[arg-type]
        )
    )
    stub._gate_pr_remediation_stood_down = ctrl.gate_stood_down
    stub._gate_pr_remediation_merge_block = ctrl.gate_merge_block
    # Terminal-truth arms (merged/landed/closed) evict on git/host fact and the
    # post-evicting arms (branch-resolved, preflight) are covered elsewhere.
    not_marker_gated = {
        PrRemediationState.MERGED,
        PrRemediationState.LANDED,
        PrRemediationState.HOST_CLOSED_UNMERGED,
        PrRemediationState.BRANCH_RESOLVED,
        PrRemediationState.PREFLIGHT_CLEAN,
    }
    ctx = _ctx(f"Remediate PR #25: {_REPLY_COMPLETION_PROBLEM_MARKER}", "b25")
    arms = [
        fn for state, fn in _DEQUEUE_GATE_TABLE if state not in not_marker_gated
    ]
    assert len(arms) == 3
    for gate_fn in arms:
        assert await gate_fn(ctrl, ctx) is False
    stub.observe_pr_readiness.assert_not_called()
    stub._probe_pr_merge_block.assert_not_awaited()
    stub._vcs.rev_parse.assert_not_called()
