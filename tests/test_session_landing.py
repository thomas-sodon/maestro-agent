"""Tests for the post-session mechanical landing actuator.

Covers the pure :func:`maestro.session_landing.land_uncommitted_edits` and its
:func:`is_owned_landing_branch` helper. Every effect is an injected seam, so the
tests wire plain fakes (call spies) and assert on the returned
:class:`LandOutcome` plus which seams ran — no ``mem_fs``, no VCS, no network,
no async marker (the function is synchronous and pure).

The seam-order contract the tests pin (acceptance criteria 1-2):

* GREEN gate → commit then push, and push runs only when the oracle permits.
* RED gate → rewind the working tree, **zero** push calls.
* INHERITED RED gate → hold the tree: no rewind, no commit, no push.
* push failure / unconfirmed advance → a typed outcome, never a raise.
* no working-tree edits → no gate run, no push.
* non-owned branch → skip: no gate run, no push.
* oracle refuses → committed but **zero** push calls.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.evolution import QualityGateResult
from maestro.pr_facts import MergeTruth
from maestro.session_landing import (
    LAND_COMMIT_MESSAGE,
    UNDELIVERED_STATUSES,
    GateVerdict,
    LandOutcome,
    LandSeams,
    LandStatus,
    PushResult,
    is_owned_landing_branch,
    land_uncommitted_edits,
)
from tests.conftest import MockQueryResult

_GREEN_GATES = QualityGateResult(
    passed=True, ruff_ok=True, mypy_ok=True, tests_ok=True, raw_output="ok",
)
_RED_GATES = QualityGateResult(
    passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True, raw_output="ruff",
)

_OWNED_BRANCH = "maestro/evolve/abc123"
_TASK_BRANCH = "maestro/task/run1"


class _SeamSpy:
    """Records calls to each seam so tests can assert what ran and in order."""

    def __init__(
        self,
        *,
        has_edits: bool = True,
        verdict: GateVerdict = GateVerdict.GREEN,
        can_push: bool = True,
        push_result: PushResult = PushResult.PUSHED,
    ) -> None:
        self._has_edits = has_edits
        self._verdict = verdict
        self._can_push = can_push
        self._push_result = push_result
        self.calls: list[str] = []
        self.commit_messages: list[str] = []
        self.pushed_branches: list[str] = []
        self.can_push_branches: list[str] = []

    def has_edits(self) -> bool:
        self.calls.append("has_edits")
        return self._has_edits

    def run_gates(self) -> GateVerdict:
        self.calls.append("run_gates")
        return self._verdict

    def commit(self, message: str) -> None:
        self.calls.append("commit")
        self.commit_messages.append(message)

    def can_push(self, branch: str) -> bool:
        self.calls.append("can_push")
        self.can_push_branches.append(branch)
        return self._can_push

    def push(self, branch: str) -> PushResult:
        self.calls.append("push")
        self.pushed_branches.append(branch)
        return self._push_result

    def rewind(self) -> None:
        self.calls.append("rewind")

    def seams(self) -> LandSeams:
        return LandSeams(
            has_edits=self.has_edits,
            run_gates=self.run_gates,
            commit=self.commit,
            can_push=self.can_push,
            push=self.push,
            rewind=self.rewind,
        )


# ---------------------------------------------------------------------------
# is_owned_landing_branch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("branch", [_OWNED_BRANCH, _TASK_BRANCH])
def test_owned_landing_branch_true(branch: str) -> None:
    """A ``maestro/{task,evolve}/*`` branch is an owned landing target."""
    assert is_owned_landing_branch(branch) is True


@pytest.mark.parametrize("branch", ["main", "master", "", "feature/x", "maestro/"])
def test_owned_landing_branch_false(branch: str) -> None:
    """Any non-``maestro/{task,evolve}/*`` ref is refused as a landing target."""
    assert is_owned_landing_branch(branch) is False


# ---------------------------------------------------------------------------
# land_uncommitted_edits — GREEN → commit + push (criterion 1)
# ---------------------------------------------------------------------------


def test_green_commits_then_pushes() -> None:
    """GREEN gate: commit with the deterministic message, then push once."""
    spy = _SeamSpy(verdict=GateVerdict.GREEN)
    outcome = land_uncommitted_edits(_OWNED_BRANCH, spy.seams())
    assert outcome == LandOutcome(
        status=LandStatus.LANDED,
        pushed=True,
        committed=True,
        gates_ran=True,
    )
    assert spy.calls == ["has_edits", "run_gates", "commit", "can_push", "push"]
    assert spy.commit_messages == [LAND_COMMIT_MESSAGE]
    assert spy.pushed_branches == [_OWNED_BRANCH]


# ---------------------------------------------------------------------------
# land_uncommitted_edits — RED → rewind, zero push (criterion 1)
# ---------------------------------------------------------------------------


def test_red_rewinds_and_never_pushes() -> None:
    """RED gate: rewind the working tree, make zero commit and push calls."""
    spy = _SeamSpy(verdict=GateVerdict.RED)
    outcome = land_uncommitted_edits(_OWNED_BRANCH, spy.seams())
    assert outcome == LandOutcome(
        status=LandStatus.REWOUND,
        pushed=False,
        committed=False,
        gates_ran=True,
    )
    assert spy.calls == ["has_edits", "run_gates", "rewind"]
    assert spy.pushed_branches == []
    assert "commit" not in spy.calls
    assert "push" not in spy.calls


def test_inherited_red_holds_tree() -> None:
    """INHERITED red (#4313): hold the tree — no rewind, commit, or push."""
    spy = _SeamSpy(verdict=GateVerdict.INHERITED_RED)
    outcome = land_uncommitted_edits(_OWNED_BRANCH, spy.seams())
    assert outcome == LandOutcome(
        status=LandStatus.HELD,
        pushed=False,
        committed=False,
        gates_ran=True,
    )
    assert spy.calls == ["has_edits", "run_gates"]


@pytest.mark.parametrize(
    ("push_result", "status"),
    [
        (PushResult.FAILED, LandStatus.PUSH_FAILED),
        (PushResult.UNCONFIRMED, LandStatus.PUSH_UNCONFIRMED),
    ],
)
def test_push_failed_or_unconfirmed_is_typed(
    push_result: PushResult, status: LandStatus,
) -> None:
    """A failed or unconfirmed push (#4313) is a typed outcome, not pushed."""
    spy = _SeamSpy(push_result=push_result)
    outcome = land_uncommitted_edits(_OWNED_BRANCH, spy.seams())
    assert outcome == LandOutcome(
        status=status,
        pushed=False,
        committed=True,
        gates_ran=True,
    )
    assert spy.calls == ["has_edits", "run_gates", "commit", "can_push", "push"]


# ---------------------------------------------------------------------------
# land_uncommitted_edits — no edits → no-op (criterion 2)
# ---------------------------------------------------------------------------


def test_no_edits_is_noop() -> None:
    """No working-tree edits: no gate run, no commit, no push."""
    spy = _SeamSpy(has_edits=False)
    outcome = land_uncommitted_edits(_OWNED_BRANCH, spy.seams())
    assert outcome == LandOutcome(
        status=LandStatus.NO_EDITS,
        pushed=False,
        committed=False,
        gates_ran=False,
    )
    assert spy.calls == ["has_edits"]
    assert "run_gates" not in spy.calls
    assert "push" not in spy.calls


# ---------------------------------------------------------------------------
# land_uncommitted_edits — non-owned branch → skip (criterion 2)
# ---------------------------------------------------------------------------


def test_non_owned_branch_skips_before_any_seam() -> None:
    """A non-owned branch skips before status, gate, or push are touched."""
    spy = _SeamSpy()
    outcome = land_uncommitted_edits("main", spy.seams())
    assert outcome == LandOutcome(
        status=LandStatus.NOT_OWNED,
        pushed=False,
        committed=False,
        gates_ran=False,
    )
    assert spy.calls == []


# ---------------------------------------------------------------------------
# land_uncommitted_edits — oracle refusal → zero push (criterion 2)
# ---------------------------------------------------------------------------


def test_oracle_refusal_commits_but_never_pushes() -> None:
    """Oracle refuses: gates ran GREEN, committed, but zero push calls."""
    spy = _SeamSpy(verdict=GateVerdict.GREEN, can_push=False)
    outcome = land_uncommitted_edits(_OWNED_BRANCH, spy.seams())
    assert outcome == LandOutcome(
        status=LandStatus.PUSH_REFUSED,
        pushed=False,
        committed=True,
        gates_ran=True,
    )
    assert spy.calls == ["has_edits", "run_gates", "commit", "can_push"]
    assert spy.can_push_branches == [_OWNED_BRANCH]
    assert spy.pushed_branches == []
    assert "push" not in spy.calls


# ---------------------------------------------------------------------------
# MaestroAgent._run_session_landing — seam wiring + memo grounding (criterion 3)
# ---------------------------------------------------------------------------


def _merge_truth(pr_num: int, *, settled: bool) -> MergeTruth:
    return MergeTruth(
        pr_number=pr_num,
        settled=settled,
        state="MERGED" if settled else "OPEN",
        branch_landed=settled,
        source="host_api",
        observed_at=0.0,
    )


def _prime_landing_seams(
    agent: object, branch: str, *, edits: bool = True, green: bool = True,
    owned: bool = True,
) -> None:
    """Wire the agent's landing seams for a direct ``_run_session_landing`` call."""
    agent._vcs.has_changes = MagicMock(return_value=edits)  # type: ignore[attr-defined]
    agent._vcs.stage_all = MagicMock()  # type: ignore[attr-defined]
    agent._vcs.commit = MagicMock()  # type: ignore[attr-defined]
    agent._vcs.push = MagicMock()  # type: ignore[attr-defined]
    agent._vcs.discard_changes = MagicMock()  # type: ignore[attr-defined]
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[attr-defined]
        return_value=_GREEN_GATES if green else _RED_GATES,
    )
    agent._gate_allowed_branches = MagicMock(  # type: ignore[attr-defined]
        return_value=frozenset({branch}) if owned else frozenset(),
    )
    # The push adapter binds delivery to a confirmed remote-head advance: the
    # local head and the fetched ``origin/<branch>`` agree by default.
    agent._vcs.rev_parse = MagicMock(return_value="c" * 40)  # type: ignore[attr-defined]
    agent._vcs.fetch = MagicMock()  # type: ignore[attr-defined]


def test_run_session_landing_green_pushes_and_drops_memo(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A GREEN owned edit commits+pushes and drops the merge-truth memo so the
    downstream ``is_pr_settled`` read re-fetches the just-pushed head."""
    agent = make_agent()
    branch = "maestro/evolve/ep5"
    _prime_landing_seams(agent, branch)
    agent._merge_truth_memo[5] = _merge_truth(5, settled=False)

    outcome = agent._run_session_landing(branch, 5)

    assert outcome.status is LandStatus.LANDED
    assert outcome.pushed is True
    agent._vcs.commit.assert_called_once_with(LAND_COMMIT_MESSAGE)
    agent._vcs.stage_all.assert_called_once_with()
    agent._vcs.push.assert_called_once_with(branch)
    # Memo dropped → §4c's chokepoint read re-fetches the pushed origin head.
    assert 5 not in agent._merge_truth_memo


def test_run_session_landing_oracle_refusal_retains_memo(make_agent) -> None:  # type: ignore[no-untyped-def]
    """When the owned-branches oracle refuses, the raw push never runs and the
    merge-truth memo is untouched (nothing landed, nothing to re-ground)."""
    agent = make_agent()
    branch = "maestro/evolve/ep5"
    _prime_landing_seams(agent, branch, owned=False)
    truth = _merge_truth(5, settled=False)
    agent._merge_truth_memo[5] = truth

    outcome = agent._run_session_landing(branch, 5)

    assert outcome.status is LandStatus.PUSH_REFUSED
    agent._vcs.push.assert_not_called()
    assert agent._merge_truth_memo[5] is truth


def test_run_session_landing_red_rewinds_and_keeps_memo(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A RED gate rewinds the working tree, never pushes, and keeps the memo."""
    agent = make_agent()
    branch = "maestro/evolve/ep5"
    _prime_landing_seams(agent, branch, green=False)
    truth = _merge_truth(5, settled=False)
    agent._merge_truth_memo[5] = truth

    outcome = agent._run_session_landing(branch, 5)

    assert outcome.status is LandStatus.REWOUND
    agent._vcs.discard_changes.assert_called_once_with()
    agent._vcs.push.assert_not_called()
    assert agent._merge_truth_memo[5] is truth


def test_run_session_landing_inherited_red_holds_tree(make_agent) -> None:  # type: ignore[no-untyped-def]
    """REGRESSION (#4313): an INHERITED red holds the tree — no
    ``discard_changes``, nothing committed or pushed, memo retained."""
    agent = make_agent()
    branch = "maestro/evolve/ep5"
    _prime_landing_seams(agent, branch, green=False)
    agent._attribute_owned_gate_red = MagicMock(return_value="INHERITED")  # type: ignore[method-assign]
    truth = _merge_truth(5, settled=False)
    agent._merge_truth_memo[5] = truth

    outcome = agent._run_session_landing(branch, 5)

    assert outcome.status is LandStatus.HELD
    agent._attribute_owned_gate_red.assert_called_once_with(_RED_GATES)
    agent._vcs.discard_changes.assert_not_called()
    agent._vcs.commit.assert_not_called()
    agent._vcs.push.assert_not_called()
    assert agent._merge_truth_memo[5] is truth


def test_run_session_landing_push_error_is_recorded_not_raised(make_agent) -> None:  # type: ignore[no-untyped-def]
    """REGRESSION (#4313): a raising push is recorded ``PUSH_FAILED``."""
    from maestro.vcs import VCSError

    agent = make_agent()
    branch = "maestro/evolve/ep5"
    _prime_landing_seams(agent, branch)
    agent._vcs.push = MagicMock(side_effect=VCSError("rejected"))  # type: ignore[attr-defined]
    truth = _merge_truth(5, settled=False)
    agent._merge_truth_memo[5] = truth

    outcome = agent._run_session_landing(branch, 5)

    assert outcome.status is LandStatus.PUSH_FAILED
    assert outcome.committed is True
    assert outcome.pushed is False
    assert agent._merge_truth_memo[5] is truth


def test_run_session_landing_push_unconfirmed(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A push whose remote head never advances is ``PUSH_UNCONFIRMED``."""
    agent = make_agent()
    branch = "maestro/evolve/ep5"
    _prime_landing_seams(agent, branch)

    def _rev_parse(ref: str) -> str:
        return "d" * 40 if ref.startswith("origin/") else "c" * 40

    agent._vcs.rev_parse = MagicMock(side_effect=_rev_parse)  # type: ignore[attr-defined]
    truth = _merge_truth(5, settled=False)
    agent._merge_truth_memo[5] = truth

    outcome = agent._run_session_landing(branch, 5)

    assert outcome.status is LandStatus.PUSH_UNCONFIRMED
    assert outcome.pushed is False
    assert agent._merge_truth_memo[5] is truth


# ---------------------------------------------------------------------------
# run() wiring — flag gates the actuator (criterion 3)
# ---------------------------------------------------------------------------


async def _drive_remediation_run(
    agent: object, goal: str, pr_branch: str,
    on_exec: Callable[[], None] | None = None,
) -> None:
    # The 4c host-state stamp gate (which runs *after* the actuator block) probes
    # merge state; stub it to an unlanded read so run() never shells out to git.
    agent._probe_pr_merge_block = AsyncMock(  # type: ignore[attr-defined]
        return_value=(False, "AWAITING_PUSH"),
    )
    plan_response = json.dumps([{"description": "Author the fix"}])
    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock) as mock_distill:
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Done.")
        if on_exec is not None:
            def _exec_side_effect(*_a: object, **_k: object) -> MockQueryResult:
                on_exec()
                return MockQueryResult(response="Done.")

            mock_exec.side_effect = _exec_side_effect
        mock_distill.return_value = MockQueryResult(response="Findings")
        await agent.run(goal, pr_branch=pr_branch, source="external")  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_invokes_actuator_when_enabled(make_agent) -> None:  # type: ignore[no-untyped-def]
    """With the flag on, run() invokes the session-landing actuator after the
    remediation session with the PR branch and parsed PR number."""
    agent = make_agent()
    agent.config.session_landing_enabled = True
    spy = MagicMock(return_value=None)
    agent._run_session_landing = spy  # type: ignore[method-assign]

    await _drive_remediation_run(
        agent, "Remediate PR #7: fix the thing", "maestro/evolve/p7",
    )

    spy.assert_called_once_with("maestro/evolve/p7", 7)


@pytest.mark.asyncio
async def test_run_skips_actuator_when_disabled(make_agent) -> None:  # type: ignore[no-untyped-def]
    """With the flag off (default), run() never invokes the actuator — the
    legacy commit-only path is unchanged."""
    agent = make_agent()
    assert agent.config.session_landing_enabled is False
    spy = MagicMock(return_value=None)
    agent._run_session_landing = spy  # type: ignore[method-assign]

    await _drive_remediation_run(
        agent, "Remediate PR #7: fix the thing", "maestro/evolve/p7",
    )

    spy.assert_not_called()


@pytest.mark.asyncio
async def test_run_skips_actuator_on_checkout_fallback(make_agent) -> None:  # type: ignore[no-untyped-def]
    """When the PR-branch checkout falls back (run stays on ``main``,
    ``branch_name`` is None), the actuator is NOT invoked — committing there and
    pushing the PR branch would land task edits on the wrong branch (mirrors the
    §5-push ``branch_name == pr_branch`` guard)."""
    from maestro.vcs import VCSError

    agent = make_agent()
    agent.config.session_landing_enabled = True
    # The checkout fallback leaves the §5-push never-attempted, which under the
    # live marker-delivery default probes reachability; this test is about the
    # landing actuator, not the marker taxonomy, so mock the fetch seam.
    agent._fetch_origin_for_merge_truth = MagicMock()  # type: ignore[method-assign]
    spy = MagicMock(return_value=None)
    agent._run_session_landing = spy  # type: ignore[method-assign]

    pr_branch = "maestro/evolve/p7"

    def _checkout(name: str) -> None:
        # Entry-time ``checkout("main")`` succeeds; the PR-branch and its origin
        # fallback both fail so run() clears ``branch_name`` to None.
        if pr_branch in name:
            raise VCSError("not local")

    agent._vcs.checkout.side_effect = _checkout
    agent._vcs.checkout_new_branch.side_effect = VCSError("no origin ref")

    await _drive_remediation_run(
        agent, "Remediate PR #7: fix the thing", pr_branch,
    )

    spy.assert_not_called()


# ---------------------------------------------------------------------------
# run() ordering — the actuator push must NOT poison the §4c pr_head_sha_stamp
# (PR #3155, review comment 3622152203)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_actuator_push_does_not_poison_pr_head_sha_stamp(make_agent) -> None:  # type: ignore[no-untyped-def]
    """REGRESSION (PR #3155, comment 3622152203).

    The §4b-ii session-landing actuator runs the **real** push, which advances
    the local remote-tracking ref ``origin/<pr_branch>``. §4c then captures
    ``pr_head_sha_stamp = rev_parse("origin/<pr_branch>")`` — and because that
    capture runs *after* the actuator, it resolves to the just-pushed head
    instead of this lap's *pre-push* starting head. The recorded
    ``task.pr_head_sha`` is meant to be a *proper ancestor* of the PR's later
    origin head (see the field docstring in ``memory.py``) so the
    branch-resolved / stalled-lineage latches can tell a genuinely-advancing lap
    from a same-SHA no-op. A post-push stamp equals the current origin head, so
    the latch never decays — the exact failure the §4c comment forbids.

    This test runs the **real** actuator (not a stub), lands a push, and asserts
    the recorded ``pr_head_sha`` is the PRE-push head. It fails on the current
    tree because the capture is post-push.
    """
    agent = make_agent()
    agent.config.session_landing_enabled = True
    branch = "maestro/evolve/p7"
    # GREEN + owned + edits present → the real actuator commits and pushes.
    _prime_landing_seams(agent, branch)

    pre_push_sha = "a" * 40
    post_push_sha = "b" * 40
    pushed = {"done": False}

    def _push(_branch: str) -> None:
        # A real ``git push origin <branch>`` advances the local
        # ``refs/remotes/origin/<branch>`` tracking ref; model that here.
        pushed["done"] = True

    agent._vcs.push = MagicMock(side_effect=_push)  # type: ignore[attr-defined]

    def _rev_parse(ref: str) -> str:
        if ref == f"origin/{branch}":
            return post_push_sha if pushed["done"] else pre_push_sha
        if ref == branch:
            return post_push_sha
        return "0" * 40

    agent._vcs.rev_parse = MagicMock(side_effect=_rev_parse)  # type: ignore[attr-defined]

    await _drive_remediation_run(
        agent, "Remediate PR #7: fix the thing", branch,
    )

    # The actuator must have actually pushed (real seam, not stubbed). run()
    # also pushes again at §5-push, so ``push`` is called more than once — the
    # load-bearing fact is that the actuator's push (the first one) fired and
    # advanced the modelled origin ref before §4c captured the stamp.
    assert pushed["done"] is True
    assert agent._vcs.push.call_args_list[0].args == (branch,)

    remediation = next(
        t for t in agent.memory.get_recent_tasks(10)
        if (t.goal or "").startswith("Remediate PR #7:")
    )
    # The watermark must be this lap's PRE-push origin head so the
    # branch-resolved latch still decays once the head advances.
    assert remediation.pr_head_sha == pre_push_sha


@pytest.mark.asyncio
async def test_agent_in_session_push_does_not_poison_pr_head_sha_stamp(make_agent) -> None:  # type: ignore[no-untyped-def]
    """REGRESSION (#4567): the agent's own Bash ``git push origin HEAD:<branch>``
    during step execution advances the local ``origin/<branch>`` tracking ref.
    The recorded ``pr_head_sha`` must still be the lap-START head, read before
    step execution — not the post-push head (actuator flag left off)."""
    agent = make_agent()
    assert agent.config.session_landing_enabled is False
    branch = "maestro/evolve/p7"
    lap_start_sha = "a" * 40
    post_push_sha = "b" * 40
    pushed = {"done": False}

    def _rev_parse(ref: str) -> str:
        if ref == f"origin/{branch}":
            return post_push_sha if pushed["done"] else lap_start_sha
        return "0" * 40

    agent._vcs.rev_parse = MagicMock(side_effect=_rev_parse)  # type: ignore[attr-defined]

    def _agent_pushes() -> None:
        pushed["done"] = True

    await _drive_remediation_run(
        agent, "Remediate PR #7: fix the thing", branch, on_exec=_agent_pushes,
    )

    assert pushed["done"] is True
    remediation = next(
        t for t in agent.memory.get_recent_tasks(10)
        if (t.goal or "").startswith("Remediate PR #7:")
    )
    assert remediation.pr_head_sha == lap_start_sha


def test_run_session_landing_push_failure_logs_error_at_warning(  # type: ignore[no-untyped-def]
    make_agent, caplog,
) -> None:
    """A failed push surfaces the git error text in the WARNING log line."""
    import logging

    from maestro.vcs import VCSError

    agent = make_agent()
    branch = "maestro/evolve/ep5"
    _prime_landing_seams(agent, branch)
    agent._vcs.push = MagicMock(side_effect=VCSError("remote rejected xyz"))  # type: ignore[attr-defined]

    with caplog.at_level(logging.INFO, logger="maestro.agent"):
        outcome = agent._run_session_landing(branch, 5)

    assert outcome.status is LandStatus.PUSH_FAILED
    records = [r for r in caplog.records if "Session-landing actuator" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "remote rejected xyz" in records[0].getMessage()


def test_push_refused_is_undelivered() -> None:
    """An oracle-refused push strands a local commit — logged as undelivered."""
    assert LandStatus.PUSH_REFUSED in UNDELIVERED_STATUSES
    assert LandStatus.LANDED not in UNDELIVERED_STATUSES
