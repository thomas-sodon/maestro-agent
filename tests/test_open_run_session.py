"""Tests for the extracted ``maestro.loop.lifecycle.open_run_session``.

This is ``run``'s inline run-scoped SDK session-open block lifted into one
named, independently-testable stage (proposal 106). At the top of ``run``'s
``try``, when ``config.enable_session_reuse`` is set, ``run`` opened the
run-scoped session that ``planner.decompose`` / ``planner.replan`` /
``_distill_findings`` share via ``create_session``; otherwise it left
``run_session`` ``None``. The paired ``disconnect_session`` teardown stays in
``run``'s ``finally`` and is not exercised here.

The extraction is behaviour-preserving, so these tests pin the moved behaviour
directly on a bare ``make_agent()`` agent — no ``agent.run`` — mirroring
``tests/test_begin_task_run.py``. ``create_session`` is patched at the
``maestro.agent`` module-level binding (the import ``run`` calls through), not
``maestro.sdk``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from maestro.loop.lifecycle import open_run_session
from maestro.sdk import QueryOptions


@pytest.mark.asyncio
async def test_open_run_session_returns_none_when_reuse_disabled(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(a) Flag off (the fixture default) returns ``None`` and never opens one."""
    agent = make_agent()
    assert agent.config.enable_session_reuse is False

    with patch("maestro.loop.lifecycle.create_session", new=AsyncMock()) as create:
        result = await open_run_session(agent, identity_context="identity")

    assert result is None
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_run_session_creates_session_when_reuse_enabled(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(b) Flag on opens the run-scoped session with the pinned options."""
    agent = make_agent()
    agent.config.enable_session_reuse = True

    sentinel = object()
    with patch(
        "maestro.loop.lifecycle.create_session",
        new=AsyncMock(return_value=sentinel),
    ) as create:
        result = await open_run_session(agent, identity_context="identity")

    assert result is sentinel
    create.assert_awaited_once_with(
        system="identity",
        options=QueryOptions(effort="medium", tools=[], caller="run_session"),
        phase="run_task",
    )
