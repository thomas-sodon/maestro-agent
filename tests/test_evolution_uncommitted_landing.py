"""Uncommitted-edit arm of the ahead-of-origin push actuator.

The committed-but-unpushed ahead-of-origin actuator only rescues work that
already reached a commit; the *uncommitted* working-tree edit — the fix authored
in the tree yet never committed, the deepest ``read-substitutes-for-write``
failure — is invisible to its ``rev-list`` count. These tests pin the arm that
closes that blind spot: at record time, before any external-quiet verdict is
composed, a gate-clean uncommitted diff on an oracle-owned branch is committed
with a deterministic message and delivered via the same oracle-gated
``_push_owned_branch`` (marker re-derived fetch-first), while a RED gate is
``discard_changes`` rewound (reset + ``git clean -fd``, so untracked files are
removed too) with **no** push — never a red head. Every external is mocked
(``mem_fs``, ``_vcs`` subprocess, the gate runner, ``is_pr_settled``), so
nothing shells out.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from maestro.evolution import QualityGateResult
from maestro.gate_observation import (
    GateFailure,
    GateObservation,
    GateRunObservation,
)
from maestro.loop.pr_remediation import _UNCOMMITTED_LAND_COMMIT_MESSAGE, _OwnedPushResult
from maestro.memory import TaskRecord
from maestro.pr_facts import MergeTruth
from maestro.pr_remediation_state import Marker
from maestro.remediation_tracker import find_stand_down


def _gate_result(passed: bool) -> QualityGateResult:
    """A structured quality-gate outcome — all sub-gates agree with *passed*."""
    return QualityGateResult(
        passed=passed,
        ruff_ok=passed,
        mypy_ok=passed,
        tests_ok=passed,
        raw_output="",
    )


def _red_observation() -> GateRunObservation:
    """A one-failure red pytest observation whose failure sits in a base file."""
    failure = GateFailure(
        tool="tests",
        kind="FAILED",
        location="maestro/base_module.py",
        identifier="tests/test_base.py::test_x",
        message="assert 1 == 2",
    )
    return GateRunObservation(
        gates=(
            GateObservation(
                tool="tests",
                kind="FAILED",
                failures=(failure,),
                output="FAILED tests/test_base.py::test_x",
            ),
        ),
    )


def _red_result_with_obs() -> QualityGateResult:
    """A red gate result carrying the observation the proposal-124 attributor reads."""
    return QualityGateResult(
        passed=False,
        ruff_ok=True,
        mypy_ok=True,
        tests_ok=False,
        raw_output="",
        observation=_red_observation(),
    )


def _arm_attribution(
    agent: object,
    *,
    changed_paths: tuple[str, ...],
    baseline: GateRunObservation | None,
) -> None:
    """Turn on gate observation and wire the VCS/baseline seams the reader uses.

    ``_attribute_owned_gate_red`` reads ``get_head_hash`` → ``merge_base`` →
    ``changed_paths`` off ``_vcs`` and ``evolution._observe_baseline`` for the
    base-commit baseline; the verdict is then a pure ``attribute`` over the
    observation, the *changed_paths*, and the *baseline*.
    """
    agent.config.gate_observation_enabled = True  # type: ignore[attr-defined]
    agent._vcs.get_head_hash.return_value = "headsha"  # type: ignore[attr-defined]
    agent._vcs.merge_base.return_value = "basesha"  # type: ignore[attr-defined]
    agent._vcs.changed_paths.return_value = changed_paths  # type: ignore[attr-defined]
    agent.evolution._observe_baseline = MagicMock(  # type: ignore[attr-defined]
        return_value=baseline,
    )


def _merge_truth(pr_num: int, *, settled: bool) -> MergeTruth:
    """A provenance-stamped merge-state fact for the re-derivation seam."""
    return MergeTruth(
        pr_number=pr_num,
        settled=settled,
        state="MERGED" if settled else "OPEN",
        branch_landed=settled,
        source="host_api",
        observed_at=0.0,
    )


def _stood_down_record(pr_num: int) -> TaskRecord:
    """A chosen ``[stood-down]`` remediation lap for PR #pr_num (no commit)."""
    return TaskRecord(
        task_id=f"sd{pr_num}",
        goal=f"Remediate PR #{pr_num}: address comments",
        plan=[],
        outcome="partial",
        result_summary=f"[stood-down] awaiting external approval on PR #{pr_num}.",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-07-18T00:00:00+00:00",
        source="external",
        key_findings="[stood-down] chose silence",
    )


def _empty_record(pr_num: int) -> TaskRecord:
    """A remediation lap whose summary/findings are empty (ternary ``else`` arm)."""
    return TaskRecord(
        task_id=f"e{pr_num}",
        goal=f"Remediate PR #{pr_num}: address comments",
        plan=[],
        outcome="partial",
        result_summary="",
        errors=[],
        duration_secs=0.0,
        timestamp="2026-07-18T00:00:00+00:00",
        source="external",
        key_findings=None,
    )


def _own_branch(agent: object, pr_num: int, branch: str) -> None:
    """Register *branch* as an owned-writable evolution head (host + ledger)."""
    agent.evolution._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        branch: object(),
    }
    agent.pr_ledger.observe_head(pr_num, branch, "deadbeef")  # type: ignore[attr-defined]
    # The uncommitted arm only acts on the checked-out PR branch (#4043).
    agent._vcs.get_current_branch.return_value = branch  # type: ignore[attr-defined]


def _arm_stand_down(agent: object) -> None:
    """Disable the user-request and salvage vetoes so the guard is the sole path."""
    agent.config.user_request_blocks_standdown_enabled = False  # type: ignore[attr-defined]
    agent.config.salvage_before_disengage_enabled = False  # type: ignore[attr-defined]
    agent._fetch_origin_for_merge_truth = MagicMock()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Detector: _branch_has_uncommitted_owned_edit
# ---------------------------------------------------------------------------


def test_detector_true_when_owned_and_dirty(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An oracle-owned branch with a dirty working tree is detected."""
    agent = make_agent()
    branch = "maestro/evolve/ep600"
    _own_branch(agent, 600, branch)
    agent._vcs.has_changes.return_value = True

    assert agent._branch_has_uncommitted_owned_edit(branch) is True


def test_detector_false_when_not_owned(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A non-owned branch short-circuits to False without reading the tree."""
    agent = make_agent()
    agent.evolution._host.get_open_evolution_pr_states.return_value = {}
    agent._vcs.has_changes.return_value = True

    assert agent._branch_has_uncommitted_owned_edit("maestro/evolve/foreign") is False
    # The oracle refused before the working-tree read — a foreign dirty tree
    # never triggers a commit+push.
    agent._vcs.has_changes.assert_not_called()


def test_detector_false_when_owned_but_clean(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An owned branch with a clean working tree is not an uncommitted edit."""
    agent = make_agent()
    branch = "maestro/evolve/ep601"
    _own_branch(agent, 601, branch)
    agent._vcs.has_changes.return_value = False

    assert agent._branch_has_uncommitted_owned_edit(branch) is False


def test_detector_false_when_not_on_pr_branch(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Post-teardown (``main`` checked out) a dirty tree is never the PR's edit (#4043)."""
    agent = make_agent()
    branch = "maestro/evolve/ep602"
    _own_branch(agent, 602, branch)
    agent._vcs.get_current_branch.return_value = "main"
    agent._vcs.has_changes.return_value = True

    assert agent._branch_has_uncommitted_owned_edit(branch) is False
    agent._vcs.has_changes.assert_not_called()


def test_detector_false_when_current_branch_unreadable(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A detached/unreadable HEAD refuses rather than guessing."""
    from maestro.vcs import VCSError

    agent = make_agent()
    branch = "maestro/evolve/ep603"
    _own_branch(agent, 603, branch)
    agent._vcs.get_current_branch.side_effect = VCSError("detached")
    agent._vcs.has_changes.return_value = True

    assert agent._branch_has_uncommitted_owned_edit(branch) is False


@pytest.mark.asyncio
async def test_uncommitted_arm_noop_when_not_on_pr_branch(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Through the record path on ``main`` with a dirty tree: no gate, no commit,
    no discard, no push (#4043)."""
    agent = make_agent()
    branch = "maestro/evolve/ep604"
    _own_branch(agent, 604, branch)
    _arm_stand_down(agent)
    agent._vcs.get_current_branch.return_value = "main"
    agent._vcs.has_changes.return_value = True
    agent._vcs.commits_not_on_remote.return_value = 0
    agent.evolution.run_quality_gates = MagicMock()  # type: ignore[method-assign]
    agent._commit_all = MagicMock()  # type: ignore[method-assign]
    agent._push_owned_branch = MagicMock()  # type: ignore[method-assign]

    await agent._record_remediation_outcome(
        goal="Remediate PR #604: address comments",
        pr_branch=branch,
        record=_stood_down_record(604),
        head_before="abc",
    )

    agent.evolution.run_quality_gates.assert_not_called()
    agent._commit_all.assert_not_called()
    agent._vcs.discard_changes.assert_not_called()
    agent._push_owned_branch.assert_not_called()


# ---------------------------------------------------------------------------
# Actuator: _land_uncommitted_owned_edit
# ---------------------------------------------------------------------------


def test_land_red_gate_rewinds_and_never_pushes(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """RED gate → ``discard_changes`` rewind, no commit, no push, ``[red]``."""
    agent = make_agent()
    branch = "maestro/evolve/ep610"
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(False),
    )
    agent._push_owned_branch = MagicMock()  # type: ignore[method-assign]
    agent._commit_all = MagicMock()  # type: ignore[method-assign]
    record = _stood_down_record(610)

    agent._land_uncommitted_owned_edit(610, branch, record)

    # The rewind seam is ``discard_changes`` (reset + ``git clean -fd``), not a
    # bare ``reset --hard`` — only the former removes an untracked red file.
    agent._vcs.discard_changes.assert_called_once_with()
    agent._vcs.reset_hard.assert_not_called()
    agent._push_owned_branch.assert_not_called()
    agent._commit_all.assert_not_called()
    assert record.outcome == "partial"
    assert Marker.UNCOMMITTED_RED in record.result_summary
    assert Marker.UNCOMMITTED_RED in (record.key_findings or "")


def test_land_red_gate_tolerates_none_record(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A ``None`` record on the RED path is tolerated — still rewinds, no push."""
    agent = make_agent()
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(False),
    )
    agent._push_owned_branch = MagicMock()  # type: ignore[method-assign]

    agent._land_uncommitted_owned_edit(611, "maestro/evolve/ep611", None)

    agent._vcs.discard_changes.assert_called_once_with()
    agent._vcs.reset_hard.assert_not_called()
    agent._push_owned_branch.assert_not_called()


def test_land_red_gate_populates_empty_fields(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """On the RED path an empty summary/findings is replaced outright by the note."""
    agent = make_agent()
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(False),
    )
    record = _empty_record(612)

    agent._land_uncommitted_owned_edit(612, "maestro/evolve/ep612", record)

    assert record.result_summary.startswith(str(Marker.UNCOMMITTED_RED))
    assert (record.key_findings or "").startswith(str(Marker.UNCOMMITTED_RED))


def test_land_red_gate_actually_removes_untracked_file(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """RED rewind sweeps an *untracked-only* dirty tree, not just tracked mods.

    The most likely uncommitted edit is a newly authored, never-committed file —
    which is untracked. A bare ``git reset --hard HEAD`` leaves untracked files
    in place, so asserting only that ``reset_hard`` was called would give false
    confidence that the tree is clean. Here the ``_vcs`` seams carry side effects
    modelling real git semantics over an in-memory working tree: ``reset_hard``
    reverts tracked mods but *leaves* untracked files (as git does), while
    ``discard_changes`` also sweeps untracked ones (its ``git clean -fd`` step).
    The RED path must pick the sweeping seam, so the untracked artifact is gone.
    """
    agent = make_agent()
    # Model the working tree: a newly authored, never-committed (untracked) file
    # plus a tracked modification.
    untracked: set[str] = {"maestro/new_module.py"}
    tracked_dirty: set[str] = {"maestro/existing.py"}

    def _reset_hard(_ref: str) -> None:
        # git reset --hard HEAD reverts tracked changes but leaves untracked.
        tracked_dirty.clear()

    def _discard_changes() -> None:
        # reset + checkout + ``git clean -fd`` — sweeps tracked *and* untracked.
        tracked_dirty.clear()
        untracked.clear()

    agent._vcs.reset_hard.side_effect = _reset_hard
    agent._vcs.discard_changes.side_effect = _discard_changes
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(False),
    )
    agent._push_owned_branch = MagicMock()  # type: ignore[method-assign]
    record = _stood_down_record(613)

    agent._land_uncommitted_owned_edit(613, "maestro/evolve/ep613", record)

    # The untracked new file was actually removed by the rewind — a bare
    # ``reset --hard`` would have left it, failing this assertion.
    assert untracked == set()
    assert tracked_dirty == set()
    agent._vcs.discard_changes.assert_called_once_with()
    agent._push_owned_branch.assert_not_called()
    assert record.outcome == "partial"


def test_land_inherited_red_holds_tree_and_never_pushes(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """INHERITED red → hold the tree: no discard_changes, no commit/push, ``[inherited-red]``.

    The identical failure identity is present in a baseline of the branch's own
    base, so the red belongs to origin/main, not this edit's diff. The arm holds
    the working tree — never sweeping work the branch did not break — and records
    a partial naming the inherited base failures.
    """
    agent = make_agent()
    branch = "maestro/evolve/ep630"
    _arm_attribution(
        agent,
        changed_paths=("maestro/other.py",),  # the failing file is NOT in the diff
        baseline=_red_observation(),  # the base carries the identical identity
    )
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_red_result_with_obs(),
    )
    agent._push_owned_branch = MagicMock()  # type: ignore[method-assign]
    agent._commit_all = MagicMock()  # type: ignore[method-assign]
    record = _stood_down_record(630)

    agent._land_uncommitted_owned_edit(630, branch, record)

    # The tree is held: no rewind of any kind, and nothing committed or pushed.
    agent._vcs.discard_changes.assert_not_called()
    agent._vcs.reset_hard.assert_not_called()
    agent._commit_all.assert_not_called()
    agent._push_owned_branch.assert_not_called()
    assert record.outcome == "partial"
    assert Marker.INHERITED_RED in record.result_summary
    assert Marker.INHERITED_RED in (record.key_findings or "")
    assert Marker.UNCOMMITTED_RED not in record.result_summary
    # The partial names the inherited base failure identity.
    assert "tests:tests/test_base.py::test_x" in record.result_summary


def test_land_attributed_red_still_rewinds(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """ATTRIBUTED red → today's ``discard_changes`` rewind, ``[red]`` — never held.

    The failing file *is* in the diff, so the red is the branch's own and the
    unverified edit is swept exactly as before.
    """
    agent = make_agent()
    branch = "maestro/evolve/ep631"
    _arm_attribution(
        agent,
        changed_paths=("maestro/base_module.py",),  # the failing file IS in the diff
        baseline=_red_observation(),
    )
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_red_result_with_obs(),
    )
    agent._push_owned_branch = MagicMock()  # type: ignore[method-assign]
    record = _stood_down_record(631)

    agent._land_uncommitted_owned_edit(631, branch, record)

    agent._vcs.discard_changes.assert_called_once_with()
    agent._push_owned_branch.assert_not_called()
    assert Marker.UNCOMMITTED_RED in record.result_summary
    assert Marker.INHERITED_RED not in record.result_summary


def test_land_flag_off_red_rewinds_despite_observation(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag off → the observation is ignored and today's rewind path is byte-identical.

    Even with an INHERITED-shaped baseline wired, a flag-off run never reaches
    the attributor: ``_attribute_owned_gate_red`` returns ``None`` before reading
    the observation, so the baseline seam is never consulted and the rewind fires.
    """
    agent = make_agent()
    branch = "maestro/evolve/ep632"
    # Flag stays off (the ``make_agent`` default); wire the seams anyway.
    agent._vcs.get_head_hash.return_value = "headsha"
    agent._vcs.merge_base.return_value = "basesha"
    agent._vcs.changed_paths.return_value = ("maestro/other.py",)
    agent.evolution._observe_baseline = MagicMock(  # type: ignore[method-assign]
        return_value=_red_observation(),
    )
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_red_result_with_obs(),
    )
    agent._push_owned_branch = MagicMock()  # type: ignore[method-assign]
    record = _stood_down_record(632)

    agent._land_uncommitted_owned_edit(632, branch, record)

    agent._vcs.discard_changes.assert_called_once_with()
    agent._push_owned_branch.assert_not_called()
    assert Marker.UNCOMMITTED_RED in record.result_summary
    assert Marker.INHERITED_RED not in record.result_summary
    # Flag-off short-circuits before the baseline is ever read.
    agent.evolution._observe_baseline.assert_not_called()


# ---------------------------------------------------------------------------
# _attribute_owned_gate_red — the shared attribution reader
# ---------------------------------------------------------------------------


def test_attribute_owned_gate_red_none_when_flag_off(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag off → ``None`` (today's behaviour); no VCS or baseline seam is read."""
    agent = make_agent()
    assert agent._attribute_owned_gate_red(_red_result_with_obs()) is None
    agent._vcs.get_head_hash.assert_not_called()


def test_attribute_owned_gate_red_none_when_no_observation(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag on but the review carries no observation → ``None``."""
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    assert agent._attribute_owned_gate_red(_gate_result(False)) is None


def test_attribute_owned_gate_red_unattributable_without_head(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag on, observation present, but HEAD unreadable → ``UNATTRIBUTABLE``."""
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    agent._vcs.get_head_hash.return_value = None
    verdict = agent._attribute_owned_gate_red(_red_result_with_obs())
    assert verdict == "UNATTRIBUTABLE"


def test_attribute_owned_gate_red_unattributable_without_base(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag on, observation + head, but no merge-base → ``UNATTRIBUTABLE``."""
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    agent._vcs.get_head_hash.return_value = "headsha"
    agent._vcs.merge_base.return_value = None
    verdict = agent._attribute_owned_gate_red(_red_result_with_obs())
    assert verdict == "UNATTRIBUTABLE"


def test_land_green_push_delivered_reobserves(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """GREEN → commit with the deterministic message, push, re-derive fetch-first.

    An unsettled re-read stamps the honest ``delivered, awaiting merge`` marker
    and drops the stale per-iteration memo so the read re-fetches.
    """
    agent = make_agent()
    branch = "maestro/evolve/ep620"
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(True),
    )
    agent._commit_all = MagicMock()  # type: ignore[method-assign]
    agent._push_owned_branch = MagicMock(  # type: ignore[method-assign]
        return_value=_OwnedPushResult(pushed=True),
    )
    agent._merge_truth_memo[620] = _merge_truth(620, settled=True)
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_merge_truth(620, settled=False),
    )
    record = _stood_down_record(620)

    agent._land_uncommitted_owned_edit(620, branch, record)

    agent._commit_all.assert_called_once_with(_UNCOMMITTED_LAND_COMMIT_MESSAGE)
    agent._push_owned_branch.assert_called_once_with(branch)
    assert 620 not in agent._merge_truth_memo
    agent.is_pr_settled.assert_any_call(620)
    assert Marker.DELIVERED_AWAITING_MERGE in record.result_summary
    assert Marker.OWNED_PUSH_AVAILABLE not in record.result_summary
    assert record.outcome == "partial"


def test_land_green_push_settled_stamps_landed(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A pushed head git shows settled coerces to the ``[landed]`` marker."""
    agent = make_agent()
    branch = "maestro/evolve/ep621"
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(True),
    )
    agent._commit_all = MagicMock()  # type: ignore[method-assign]
    agent._push_owned_branch = MagicMock(  # type: ignore[method-assign]
        return_value=_OwnedPushResult(pushed=True),
    )
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_merge_truth(621, settled=True),
    )
    record = _stood_down_record(621)

    agent._land_uncommitted_owned_edit(621, branch, record)

    assert Marker.LANDED in record.result_summary
    assert Marker.DELIVERED_AWAITING_MERGE not in record.result_summary


def test_land_green_push_unconfirmed_stamps_partial(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A no-op push (exit 0, remote head unchanged) → ``[push-unconfirmed]``."""
    agent = make_agent()
    branch = "maestro/evolve/ep622"
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(True),
    )
    agent._commit_all = MagicMock()  # type: ignore[method-assign]
    agent._push_owned_branch = MagicMock(  # type: ignore[method-assign]
        return_value=_OwnedPushResult(pushed=False, unconfirmed=True),
    )
    record = _stood_down_record(622)

    agent._land_uncommitted_owned_edit(622, branch, record)

    assert Marker.PUSH_UNCONFIRMED in record.result_summary
    assert record.outcome == "partial"
    model = agent.consciousness.load_self_model()
    assert model.next_task_directive is not None
    assert branch in model.next_task_directive


def test_land_green_push_refused_falls_back_to_owned_push_partial(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A refused/failed push after commit → the loud OWNED_PUSH_AVAILABLE partial."""
    agent = make_agent()
    branch = "maestro/evolve/ep623"
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(True),
    )
    agent._commit_all = MagicMock()  # type: ignore[method-assign]
    agent._push_owned_branch = MagicMock(  # type: ignore[method-assign]
        return_value=_OwnedPushResult(pushed=False, error="remote rejected"),
    )
    agent._branch_ahead_of_origin_count = MagicMock(  # type: ignore[method-assign]
        return_value=1,
    )
    record = _stood_down_record(623)

    agent._land_uncommitted_owned_edit(623, branch, record)

    assert Marker.OWNED_PUSH_AVAILABLE in record.result_summary
    assert Marker.DELIVERED_AWAITING_MERGE not in record.result_summary
    model = agent.consciousness.load_self_model()
    assert model.next_task_directive is not None
    assert branch in model.next_task_directive


# ---------------------------------------------------------------------------
# Integration through _record_remediation_outcome
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_outcome_uncommitted_arm_commits_and_pushes(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """AC1: a gate-clean uncommitted diff at record time is committed then pushed
    via ``_push_owned_branch``, and the marker is re-derived via ``is_pr_settled``
    — no stand-down verdict is minted."""
    agent = make_agent()
    branch = "maestro/evolve/ep700"
    _arm_stand_down(agent)
    _own_branch(agent, 700, branch)
    agent._vcs.has_changes.return_value = True
    # No committed ahead-of-origin work: the arm fires on the *uncommitted* edit.
    agent._vcs.commits_not_on_remote.return_value = 0
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(True),
    )
    agent._commit_all = MagicMock()  # type: ignore[method-assign]
    agent._push_owned_branch = MagicMock(  # type: ignore[method-assign]
        return_value=_OwnedPushResult(pushed=True),
    )
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_merge_truth(700, settled=False),
    )
    record = _stood_down_record(700)

    await agent._record_remediation_outcome(
        goal="Remediate PR #700: address comments",
        pr_branch=branch,
        record=record,
        head_before="abc",
    )

    agent._commit_all.assert_called_once_with(_UNCOMMITTED_LAND_COMMIT_MESSAGE)
    agent._push_owned_branch.assert_called_once_with(branch)
    agent.is_pr_settled.assert_any_call(700)
    assert Marker.DELIVERED_AWAITING_MERGE in record.result_summary
    # No stand-down verdict was minted for the delivered branch.
    assert str(agent.config.memory_dir / "stand_down_verdicts.jsonl") not in mem_fs


@pytest.mark.asyncio
async def test_record_outcome_uncommitted_red_gate_rewinds(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """AC2: a RED gate on the uncommitted diff performs a ``discard_changes``
    rewind, records a ``partial`` with a ``[red]`` note, and never pushes."""
    agent = make_agent()
    branch = "maestro/evolve/ep701"
    _arm_stand_down(agent)
    _own_branch(agent, 701, branch)
    agent._vcs.has_changes.return_value = True
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(False),
    )
    agent._push_owned_branch = MagicMock()  # type: ignore[method-assign]
    record = _stood_down_record(701)

    await agent._record_remediation_outcome(
        goal="Remediate PR #701: address comments",
        pr_branch=branch,
        record=record,
        head_before="abc",
    )

    agent._vcs.discard_changes.assert_called_once_with()
    agent._vcs.reset_hard.assert_not_called()
    agent._push_owned_branch.assert_not_called()
    assert record.outcome == "partial"
    assert Marker.UNCOMMITTED_RED in record.result_summary


@pytest.mark.asyncio
async def test_record_outcome_clean_tree_preserves_committed_actuator(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """AC3: a clean working tree makes the new arm a strict no-op — the existing
    committed-but-unpushed OWNED_AHEAD push behavior is unchanged."""
    agent = make_agent()
    branch = "maestro/evolve/ep702"
    _arm_stand_down(agent)
    _own_branch(agent, 702, branch)
    # Clean tree → the uncommitted arm never fires.
    agent._vcs.has_changes.return_value = False
    # Committed, un-pushed ahead-of-origin work → the legacy actuator must run.
    agent._vcs.commits_not_on_remote.return_value = 3
    agent._land_uncommitted_owned_edit = MagicMock()  # type: ignore[method-assign]
    agent._push_owned_branch = MagicMock(  # type: ignore[method-assign]
        return_value=_OwnedPushResult(pushed=True),
    )
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_merge_truth(702, settled=False),
    )
    record = _stood_down_record(702)

    await agent._record_remediation_outcome(
        goal="Remediate PR #702: address comments",
        pr_branch=branch,
        record=record,
        head_before="abc",
    )

    # The new arm was a strict no-op; the committed-work actuator delivered.
    agent._land_uncommitted_owned_edit.assert_not_called()
    agent._push_owned_branch.assert_called_once_with(branch)
    assert Marker.DELIVERED_AWAITING_MERGE in record.result_summary
    assert find_stand_down(702, "0" * 40, agent.stand_down_verdicts) is None
