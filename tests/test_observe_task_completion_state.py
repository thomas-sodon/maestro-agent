"""Tests for the extracted
``loop.task_delivery.observe_task_completion_state``.

This is ``run``'s ``# 5a.`` + ``# 5a-ii.`` blocks lifted into one named,
independently-testable method: the pair of completion-time *observations* that
answer "what did this lap's completion actually look like?" from two disjoint
domains. The merge-readiness arm probes a kept task branch against
``origin/main`` (owned lap, ``pr_branch is None``); the readiness-snapshot arm
records a remediation PR's aggregate readiness at lap end (``pr_branch is not
None``). Both returns are pure sinks into the ``TaskRecord`` constructor, so the
lift is behaviour-preserving and these tests pin every arm of the method's two
guards — including the mutual exclusivity that two adjacent ``if`` guards
previously left as an accident of ordering.

Two arms no through-``run`` test reaches are pinned here for the first time: the
readiness-memo drop (every existing test stubs ``observe_pr_readiness`` outright,
which makes the ``pop`` unobservable) and the all-consumers-off no-op, which
needs the default-on salvage master flag turned off explicitly. No ``agent.run``
and no SDK patching — the method is called directly on a bare ``make_agent()``
agent with ``check_merge_clean`` patched and ``observe_pr_readiness`` stubbed by
attribute assignment.
"""

from __future__ import annotations

from dataclasses import asdict
from unittest.mock import MagicMock, patch

import pytest

from maestro.agent import MaestroAgent
from maestro.loop.task_delivery import observe_task_completion_state
from maestro.pr_facts import PRReadiness

#: The remediation PR the readiness arm observes.
_PR = 3106
#: The remediation branch that arm is handed — ``pr_branch is not None`` is what
#: routes a lap into it.
_PR_BRANCH = "maestro/evolve/ep3106"
#: The committed self-directed task branch the merge-clean arm probes.
_KEPT = "maestro/task/t-5a"


def _readiness(*, ready: bool, observed_at: float) -> PRReadiness:
    """One ``PRReadiness`` fact for the snapshot arm to ``asdict``.

    The dataclass is frozen with no defaults beyond ``settled``, and the suite
    has no shared factory (each file defines its own), so every field is spelled
    out. ``ready`` / ``observed_at`` are the two axes the tests distinguish a
    stale memo entry from a fresh observation on.
    """
    return PRReadiness(
        pr_number=_PR,
        branch_landed=False,
        branch_behind=False,
        branch_conflicting=False,
        ci_state="GREEN",
        unresolved_threads=0,
        unaddressed_overflow=0,
        awaiting_approval=True,
        ready_and_waiting=ready,
        source="host_api",
        observed_at=observed_at,
    )


def _arm_readiness(agent: MaestroAgent, readiness: PRReadiness) -> MagicMock:
    """Stub the readiness chokepoint and return the spy."""
    stub = MagicMock(return_value=readiness)
    agent.observe_pr_readiness = stub  # type: ignore[method-assign]
    return stub


@pytest.mark.parametrize("verdict", [True, False, None])
def test_merge_clean_probes_the_kept_branch(  # type: ignore[no-untyped-def]
    make_agent, verdict: bool | None,
) -> None:
    """(a) The ``# 5a.`` fire arm: probe the kept branch, pass the verdict through.

    A successful self-directed lap that kept a branch is probed against
    ``origin/main`` at the moment the branch is freshest. All three
    ``check_merge_clean`` verdicts — clean, conflicting, and the ``None`` the
    probe returns when it could not answer — reach the caller untouched, since
    the ``TaskRecord`` field and the promotion directive distinguish them
    (``merge_clean is True`` alone steers a promotion). The ``cwd`` is asserted
    because the probe is only meaningful against the agent's own workspace.
    """
    agent = make_agent()

    with patch(
        "maestro.loop.task_delivery.check_merge_clean", return_value=verdict,
    ) as mock_probe:
        merge_clean, snapshot = observe_task_completion_state(
            agent,
            outcome="success",
            kept_branch=_KEPT,
            pr_branch=None,
            remediation_pr_num=None,
        )

    mock_probe.assert_called_once_with(
        _KEPT, cwd=str(agent.config.workspace),
    )
    assert merge_clean is verdict
    assert snapshot is None


@pytest.mark.parametrize("outcome", ["partial", "failure"])
def test_no_merge_probe_when_the_lap_did_not_succeed(  # type: ignore[no-untyped-def]
    make_agent, outcome: str,
) -> None:
    """(b) First no-op guard: a non-``success`` outcome is never probed.

    Promotion pressure is reserved for work that actually landed a usable
    branch, so a partial or failed lap costs zero git I/O here.
    """
    agent = make_agent()

    with patch("maestro.loop.task_delivery.check_merge_clean") as mock_probe:
        merge_clean, snapshot = observe_task_completion_state(
            agent,
            outcome=outcome,  # type: ignore[arg-type]
            kept_branch=_KEPT,
            pr_branch=None,
            remediation_pr_num=None,
        )

    mock_probe.assert_not_called()
    assert merge_clean is None
    assert snapshot is None


def test_no_merge_probe_when_no_branch_was_kept(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """(c) Second no-op guard: nothing to probe without a kept branch.

    A lap whose branch was deleted (no commits) has no ref for ``merge-tree`` to
    read, so the probe would fail rather than answer.
    """
    agent = make_agent()

    with patch("maestro.loop.task_delivery.check_merge_clean") as mock_probe:
        merge_clean, snapshot = observe_task_completion_state(
            agent,
            outcome="success",
            kept_branch=None,
            pr_branch=None,
            remediation_pr_num=None,
        )

    mock_probe.assert_not_called()
    assert merge_clean is None
    assert snapshot is None


def test_no_merge_probe_for_a_remediation_lap_that_kept_a_branch(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """(d) Third no-op guard, and the two arms' mutual exclusivity.

    Reachable in production: a remediation lap with working-tree changes does
    commit and keep a branch, so ``outcome == "success" and kept_branch`` both
    hold. ``pr_branch is not None`` is what excludes it — a remediation branch is
    delivered to its PR and merged onto ``main`` by Tend PR, so a
    ``source_task_id`` promotion is meaningless for it. The same call routes into
    the readiness arm instead, which is the exclusivity made structural.
    """
    agent = make_agent()
    fresh = _readiness(ready=True, observed_at=10.0)
    stub = _arm_readiness(agent, fresh)

    with patch("maestro.loop.task_delivery.check_merge_clean") as mock_probe:
        merge_clean, snapshot = observe_task_completion_state(
            agent,
            outcome="success",
            kept_branch=_KEPT,
            pr_branch=_PR_BRANCH,
            remediation_pr_num=_PR,
        )

    mock_probe.assert_not_called()
    assert merge_clean is None
    stub.assert_called_once_with(_PR, _PR_BRANCH)
    assert snapshot == asdict(fresh)


def test_readiness_snapshot_under_the_dedicated_flag(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """(e) The ``# 5a-ii.`` fire arm via ``progress_aware_lap_budget_enabled``.

    The dedicated flag alone arms the observation — the salvage master flag is
    pinned off here so the union predicate cannot be the thing that answered.
    """
    agent = make_agent()
    agent.config.progress_aware_lap_budget_enabled = True
    agent.config.salvage_before_disengage_enabled = False
    agent.config.observed_ready_standdown_enabled = False
    fresh = _readiness(ready=False, observed_at=20.0)
    stub = _arm_readiness(agent, fresh)

    merge_clean, snapshot = observe_task_completion_state(
        agent,
        outcome="success",
        kept_branch=None,
        pr_branch=_PR_BRANCH,
        remediation_pr_num=_PR,
    )

    stub.assert_called_once_with(_PR, _PR_BRANCH)
    assert snapshot == asdict(fresh)
    assert merge_clean is None


def test_readiness_snapshot_under_the_salvage_master_flag(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """(f) The same fire arm via the default-on salvage master flag.

    ``Config.progress_aware_lap_budget_active`` is the single owner of "is the
    progress-aware lap budget active?", so flipping the salvage master flag arms
    the snapshot without the dedicated flag — the production posture, since
    ``salvage_before_disengage_enabled`` ships on.
    """
    agent = make_agent()
    assert agent.config.progress_aware_lap_budget_enabled is False
    assert agent.config.salvage_before_disengage_enabled is True
    agent.config.observed_ready_standdown_enabled = False
    fresh = _readiness(ready=True, observed_at=30.0)
    stub = _arm_readiness(agent, fresh)

    _, snapshot = observe_task_completion_state(
        agent,
        outcome="partial",
        kept_branch=None,
        pr_branch=_PR_BRANCH,
        remediation_pr_num=_PR,
    )

    stub.assert_called_once_with(_PR, _PR_BRANCH)
    assert snapshot == asdict(fresh)


def test_readiness_snapshot_under_the_observed_ready_flag(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """(g) The gate's third disjunct: the observed-readiness dequeue gate.

    That gate is the snapshot's other consumer, so it arms the observation on
    its own with the whole progress-aware union pinned off.
    """
    agent = make_agent()
    agent.config.progress_aware_lap_budget_enabled = False
    agent.config.salvage_before_disengage_enabled = False
    agent.config.observed_ready_standdown_enabled = True
    fresh = _readiness(ready=True, observed_at=40.0)
    stub = _arm_readiness(agent, fresh)

    _, snapshot = observe_task_completion_state(
        agent,
        outcome="success",
        kept_branch=None,
        pr_branch=_PR_BRANCH,
        remediation_pr_num=_PR,
    )

    stub.assert_called_once_with(_PR, _PR_BRANCH)
    assert snapshot == asdict(fresh)


def test_no_snapshot_when_the_lap_cites_no_pr(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """(h) First readiness no-op guard: a branch but no PR number to observe.

    There is nothing to ask the host about, so the observation's git/host I/O is
    skipped even with every consumer flag on.
    """
    agent = make_agent()
    agent.config.progress_aware_lap_budget_enabled = True
    agent.config.observed_ready_standdown_enabled = True
    stub = _arm_readiness(agent, _readiness(ready=True, observed_at=50.0))

    merge_clean, snapshot = observe_task_completion_state(
        agent,
        outcome="success",
        kept_branch=None,
        pr_branch=_PR_BRANCH,
        remediation_pr_num=None,
    )

    stub.assert_not_called()
    assert snapshot is None
    assert merge_clean is None


def test_no_snapshot_when_every_consumer_flag_is_off(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """(i) Second readiness no-op guard: no consumer enabled → inert.

    Turning the observation off takes **both** ``salvage_before_disengage_enabled``
    (the master flag ``progress_aware_lap_budget_active`` unions in, default-on)
    and ``observed_ready_standdown_enabled``; the fixture pins only the latter,
    so the salvage flag is what keeps this arm live in production. With all
    three off the method costs zero host I/O.
    """
    agent = make_agent()
    agent.config.progress_aware_lap_budget_enabled = False
    agent.config.salvage_before_disengage_enabled = False
    agent.config.observed_ready_standdown_enabled = False
    assert agent.config.progress_aware_lap_budget_active is False
    stub = _arm_readiness(agent, _readiness(ready=True, observed_at=60.0))

    merge_clean, snapshot = observe_task_completion_state(
        agent,
        outcome="success",
        kept_branch=None,
        pr_branch=_PR_BRANCH,
        remediation_pr_num=_PR,
    )

    stub.assert_not_called()
    assert snapshot is None
    assert merge_clean is None


def test_stale_readiness_memo_is_dropped_before_observing(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """(j) The memo drop: the snapshot is post-lap, not the dequeue-time read.

    ``observe_pr_readiness`` is memoized per iteration, and a dequeue gate has
    usually already populated that memo for this PR *before* the lap ran. Popping
    the key first is what makes the recorded snapshot reflect what the lap
    achieved rather than the state it was handed — the difference the
    consecutive-no-progress sensor reads. No existing test reaches this, because
    they all stub ``observe_pr_readiness`` and never seed the memo, so a later
    edit dropping the ``pop`` would otherwise go unnoticed.
    """
    agent = make_agent()
    stale = _readiness(ready=False, observed_at=1.0)
    fresh = _readiness(ready=True, observed_at=99.0)
    agent._pr_readiness_memo[_PR] = stale
    stub = _arm_readiness(agent, fresh)

    _, snapshot = observe_task_completion_state(
        agent,
        outcome="success",
        kept_branch=None,
        pr_branch=_PR_BRANCH,
        remediation_pr_num=_PR,
    )

    assert _PR not in agent._pr_readiness_memo
    stub.assert_called_once_with(_PR, _PR_BRANCH)
    assert snapshot == asdict(fresh)
    assert snapshot != asdict(stale)
