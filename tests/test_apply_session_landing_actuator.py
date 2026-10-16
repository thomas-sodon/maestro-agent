"""Unit test for ``loop.task_outcome.apply_session_landing_actuator``.

The stage is the verbatim lift of ``run``'s ``# 4b-ii.`` post-session
mechanical landing actuator (proposal 106): with the opt-in
``[loop] session_landing_enabled`` flag on, and only when the run is actually
on an owned PR-remediation head (``branch_name == pr_branch`` on a
``Remediate PR #N:`` goal), it delegates to ``_run_session_landing`` so the
loop's hand — not a composed sentence — commits and pushes gate-clean
working-tree edits before verdict composition. The compound inline guard
becomes early-return arms; the ``Remediate PR #N:`` parse and the delegated
signature are unchanged (the trailing-colon regex misses a non-numeric ``#x``
goal, so it still calls through with ``pr_num=None``).

These tests call the stage directly, agent-first-positional, on a bare ``make_agent``
agent (no ``agent.run()``, no SDK patching), replacing ``_run_session_landing``
with a ``MagicMock`` spy, and pin every guard arm so it holds 100 % coverage on
its own. The full-``run`` regression harness (``tests/test_session_landing.py``
``test_run_invokes_actuator_*`` / ``test_run_skips_actuator_*``) exercises the
same block end-to-end and must pass unchanged.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from maestro.loop.task_outcome import apply_session_landing_actuator

_PR_BRANCH = "maestro/evolve/p7"
_GOAL = "Remediate PR #7: address the review threads"


def _spy(agent) -> MagicMock:  # type: ignore[no-untyped-def]
    """Replace the inner landing actuator with a non-shelling spy."""
    spy = MagicMock(return_value=None)
    agent._run_session_landing = spy  # type: ignore[method-assign]
    return spy


def test_flag_off_is_noop(make_agent) -> None:  # type: ignore[no-untyped-def]
    """(a) Flag off (the ``make_agent`` default) short-circuits before the
    delegate, even when every other guard would pass."""
    agent = make_agent()
    assert agent.config.session_landing_enabled is False
    spy = _spy(agent)

    apply_session_landing_actuator(
        agent,
        pr_branch=_PR_BRANCH, branch_name=_PR_BRANCH, goal=_GOAL,
    )

    spy.assert_not_called()


def test_no_pr_branch_is_noop(make_agent) -> None:  # type: ignore[no-untyped-def]
    """(b) With the flag on but ``pr_branch is None`` (not a PR remediation),
    the actuator is never invoked."""
    agent = make_agent()
    agent.config.session_landing_enabled = True
    spy = _spy(agent)

    apply_session_landing_actuator(
        agent,
        pr_branch=None, branch_name=None, goal=_GOAL,
    )

    spy.assert_not_called()


def test_checkout_fallback_is_noop(make_agent) -> None:  # type: ignore[no-untyped-def]
    """(c) A PR-branch checkout that fell back (``branch_name != pr_branch``,
    run stayed on ``main``) short-circuits — committing there and pushing the
    PR branch would land edits on the wrong branch (mirrors §5-push)."""
    agent = make_agent()
    agent.config.session_landing_enabled = True
    spy = _spy(agent)

    apply_session_landing_actuator(
        agent,
        pr_branch=_PR_BRANCH, branch_name=None, goal=_GOAL,
    )

    spy.assert_not_called()


def test_non_remediation_goal_is_noop(make_agent) -> None:  # type: ignore[no-untyped-def]
    """(d) A goal that does not begin with the ``Remediate PR #`` prefix
    short-circuits — the actuator only lands on PR-remediation laps."""
    agent = make_agent()
    agent.config.session_landing_enabled = True
    spy = _spy(agent)

    apply_session_landing_actuator(
        agent,
        pr_branch=_PR_BRANCH,
        branch_name=_PR_BRANCH,
        goal="Improve the dashboard status layer",
    )

    spy.assert_not_called()


def test_active_path_delegates_with_parsed_pr_number(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(e) Flag on and every guard passing: the actuator parses the PR number
    and delegates to ``_run_session_landing(pr_branch, N)``."""
    agent = make_agent()
    agent.config.session_landing_enabled = True
    spy = _spy(agent)

    apply_session_landing_actuator(
        agent,
        pr_branch=_PR_BRANCH, branch_name=_PR_BRANCH, goal=_GOAL,
    )

    spy.assert_called_once_with(_PR_BRANCH, 7)


def test_malformed_number_goal_delegates_with_none(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(f) A malformed ``Remediate PR #x:`` goal clears the prefix guard but the
    trailing-colon ``\\d+`` regex misses, so the call still lands with
    ``pr_num=None``."""
    agent = make_agent()
    agent.config.session_landing_enabled = True
    spy = _spy(agent)

    apply_session_landing_actuator(
        agent,
        pr_branch=_PR_BRANCH,
        branch_name=_PR_BRANCH,
        goal="Remediate PR #x: not a real number",
    )

    spy.assert_called_once_with(_PR_BRANCH, None)
