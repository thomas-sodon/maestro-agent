"""``MaestroAgent`` forwards each moved PR-remediation method to its controller.

Proposal 147 Step 8 moved the remaining PR-remediation bodies into
:class:`maestro.loop.pr_remediation.PrRemediationController`, leaving a
same-named one-line delegator on ``MaestroAgent`` so the controller's
``agent.<name>(...)`` dispatch (and instance-level test patches) keep working.
This pins that every delegator forwards its arguments unchanged and returns the
controller's result.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from maestro.agent import MaestroAgent
from maestro.loop.pr_remediation import PrRemediationController

_DELEGATORS = (
    "_gate_pr_remediation_landed",
    "_gate_pr_remediation_ready",
    "_plan_for_pr_number",
    "_gate_pr_remediation_host_closed",
    "_escalate_stuck_pr",
    "_remediation_streak_capped",
    "_pending_pr_head_stalled",
    "_mechanical_standdown_verdict",
    "_dispose_mechanical_standdown",
    "_owned_act_drought_findings",
    "_owned_act_drought_active",
    "_preempt_yielded_slot_with_owned_prune",
    "_first_non_stalled_pr_index",
    "_refuse_owned_push_standdown",
    "_commit_all",
    "_run_session_landing",
    "_push_owned_branch",
    "_fetch_remote_branch_ok",
    "_record_owned_push_delivered",
    "_record_push_unconfirmed",
    "_branch_has_uncommitted_owned_edit",
    "_is_checked_out",
    "_attribute_owned_gate_red",
    "_land_uncommitted_owned_edit",
    "_branch_is_owned_writable",
    "_push_committed_unpushed_owned_head",
    "_settle_committed_unpushed_head",
    "_record_remediation_outcome",
    "_record_remediation_fetch_truth",
    "_record_remediation_obligation_fork",
    "_refine_ci_review_blocker",
)


def _call_arguments(func: object) -> tuple[list[object], dict[str, object]]:
    """One distinct sentinel per parameter of *func* (minus ``self``)."""
    params = list(inspect.signature(func).parameters.values())[1:]  # type: ignore[arg-type]
    args: list[object] = []
    kwargs: dict[str, object] = {}
    for param in params:
        sentinel = object()
        if param.kind is inspect.Parameter.KEYWORD_ONLY:
            kwargs[param.name] = sentinel
        else:
            args.append(sentinel)
    return args, kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _DELEGATORS)
async def test_delegator_forwards_to_controller(name: str) -> None:
    agent = object.__new__(MaestroAgent)
    controller = MagicMock(spec=PrRemediationController)
    method = getattr(MaestroAgent, name)
    is_async = inspect.iscoroutinefunction(method)
    result = object()
    target = AsyncMock(return_value=result) if is_async else MagicMock(return_value=result)
    setattr(controller, name, target)
    agent._pr_remediation = controller
    args, kwargs = _call_arguments(method)

    returned = getattr(agent, name)(*args, **kwargs)
    if is_async:
        returned = await returned

    assert returned is result
    target.assert_called_once_with(*args, **kwargs)


def test_every_delegator_is_a_controller_method() -> None:
    """Each listed name exists on both sides, so the list cannot drift silently."""
    missing = [
        name for name in _DELEGATORS
        if not hasattr(PrRemediationController, name) or not hasattr(MaestroAgent, name)
    ]
    assert missing == []
