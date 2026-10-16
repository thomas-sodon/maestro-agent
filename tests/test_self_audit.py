"""Tests for ``maestro.loop.self_audit`` — the self-audit sweep carved out of
``MaestroAgent._audit_self_model`` (proposal 147).

The sweep's rule-by-rule behaviour stays covered by the existing audit tests,
which reach it through the agent delegator; these pin the module's own entry
point and the delegator's forwarding.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from maestro.loop import self_audit


def _silence_git_audit_rules(agent) -> None:  # type: ignore[no-untyped-def]
    """Disable the git-keyed rules so the sweep needs no VCS or host."""
    agent.config.audit_impact_review_enabled = False
    agent.config.orphaned_cure_enabled = False
    agent.config.phantom_escalation_enabled = False
    agent.config.unconfirmed_landing_enabled = False
    agent.config.green_ready_handoff_enabled = False


@pytest.mark.asyncio
async def test_audit_self_model_persists_audit_result(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """A zero-finding sweep still persists one ``AuditResult`` for the cycle."""
    agent = make_agent()
    _silence_git_audit_rules(agent)

    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        await self_audit.audit_self_model(agent, iteration=7)

    results = agent.memory.get_recent_audit_results(5)
    assert len(results) == 1
    assert results[0].iteration == 7
    assert results[0].findings == []


@pytest.mark.asyncio
async def test_agent_audit_self_model_delegates_to_module(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """``MaestroAgent._audit_self_model`` forwards ``(self, iteration)``."""
    agent = make_agent()
    fake = AsyncMock(return_value=None)

    with patch("maestro.loop.self_audit.audit_self_model", fake):
        await agent._audit_self_model(iteration=3)

    fake.assert_awaited_once_with(agent, 3)
