"""Tests for the extracted ``maestro.loop.lifecycle.teardown_task_run`` stage.

The stage is the verbatim lift of ``run``'s outer ``finally`` teardown block
(proposal 106): on every exit path it disconnects the run-scoped SDK session
best-effort (the ``contextlib.suppress(Exception)`` around ``disconnect_session``
is the documented best-effort SDK-teardown contract), restores the working
tree/branch via the already-extracted ``_teardown_task_branch``, and resets the
``_running`` / ``_current_task`` flags last. The extraction is
behaviour-preserving — it lands in the ``maestro.loop.lifecycle`` sibling
rather than on ``MaestroAgent`` because ``maestro/agent.py`` was then held
shrink-only under ``tests/test_source_file_size.py`` — so these tests pin the disconnect, the
delegation to ``_teardown_task_branch`` with the passed run-scoped locals, the
flag resets, the
``run_session is None`` skip, and the ``suppress`` arm. ``_teardown_task_branch``
is shadowed with a ``MagicMock`` so nothing shells out and its own contract is
left to its own tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.loop.lifecycle import teardown_task_run


@pytest.mark.asyncio
async def test_happy_path_disconnects_tears_down_and_resets_flags(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Session present: ``disconnect_session`` awaited once, the branch teardown
    delegated with the passed locals, and the running-state flags reset."""
    agent = make_agent()
    agent._running = True
    agent._current_task = "doing the thing"
    agent._teardown_task_branch = MagicMock()  # type: ignore[method-assign]
    session = MagicMock(name="run_session")

    with patch(
        "maestro.loop.lifecycle.disconnect_session", new_callable=AsyncMock,
    ) as disconnect:
        await teardown_task_run(
            agent,
            run_session=session,
            branch_name="maestro/task/abc123",
            kept_branch="maestro/task/abc123",
            preserve_branch=True,
        )

    disconnect.assert_awaited_once_with(session)
    agent._teardown_task_branch.assert_called_once_with(
        "maestro/task/abc123", "maestro/task/abc123", True,
    )
    assert agent.is_running is False
    assert agent.current_task is None


@pytest.mark.asyncio
async def test_none_session_skips_disconnect_but_still_tears_down(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """``run_session is None``: ``disconnect_session`` is never awaited, but the
    branch teardown and the flag resets still run."""
    agent = make_agent()
    agent._running = True
    agent._current_task = "doing the thing"
    agent._teardown_task_branch = MagicMock()  # type: ignore[method-assign]

    with patch(
        "maestro.loop.lifecycle.disconnect_session", new_callable=AsyncMock,
    ) as disconnect:
        await teardown_task_run(
            agent,
            run_session=None,
            branch_name="maestro/task/abc123",
            kept_branch=None,
            preserve_branch=False,
        )

    disconnect.assert_not_awaited()
    agent._teardown_task_branch.assert_called_once_with(
        "maestro/task/abc123", None, False,
    )
    assert agent.is_running is False
    assert agent.current_task is None


@pytest.mark.asyncio
async def test_disconnect_failure_is_swallowed_and_teardown_still_runs(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The ``contextlib.suppress(Exception)`` arm: a raising ``disconnect_session``
    is swallowed and the branch teardown + flag resets still run."""
    agent = make_agent()
    agent._running = True
    agent._current_task = "doing the thing"
    agent._teardown_task_branch = MagicMock()  # type: ignore[method-assign]
    session = MagicMock(name="run_session")
    raising_disconnect = AsyncMock(side_effect=RuntimeError("torn down"))

    with patch("maestro.loop.lifecycle.disconnect_session", raising_disconnect):
        # Must not raise despite the disconnect blowing up.
        await teardown_task_run(
            agent,
            run_session=session,
            branch_name="maestro/task/abc123",
            kept_branch=None,
            preserve_branch=False,
        )

    raising_disconnect.assert_awaited_once_with(session)
    agent._teardown_task_branch.assert_called_once_with(
        "maestro/task/abc123", None, False,
    )
    assert agent.is_running is False
    assert agent.current_task is None


def _deferred_record():  # type: ignore[no-untyped-def]
    from maestro.memory import TaskRecord

    return TaskRecord(
        task_id="d1", goal="g", plan=[], outcome="partial",
        result_summary="s", errors=[], duration_secs=1.0,
        timestamp="2026-07-30T00:00:00+00:00", source="external",
    )


@pytest.mark.asyncio
async def test_handed_off_deferred_record_is_not_persisted(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A deferred record ``run`` handed off is the caller's to append (#4062)."""
    agent = make_agent()
    agent._teardown_task_branch = MagicMock()  # type: ignore[method-assign]

    await teardown_task_run(
        agent, run_session=None, branch_name=None, kept_branch=None,
        preserve_branch=False, deferred=_deferred_record(), handed_off=True,
    )

    assert agent.memory.get_recent_tasks(5) == []


@pytest.mark.asyncio
async def test_unhanded_deferred_record_is_persisted(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """``run`` raised before handing the record off: teardown appends it."""
    agent = make_agent()
    agent._teardown_task_branch = MagicMock()  # type: ignore[method-assign]

    await teardown_task_run(
        agent, run_session=None, branch_name=None, kept_branch=None,
        preserve_branch=False, deferred=_deferred_record(), handed_off=False,
    )

    assert [t.task_id for t in agent.memory.get_recent_tasks(5)] == ["d1"]


@pytest.mark.asyncio
async def test_teardown_raise_persists_handed_off_deferred_record(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Teardown itself raising (a ``CancelledError``) means the caller never
    receives the record, so it is persisted even though ``run`` handed it off."""
    import asyncio

    agent = make_agent()
    agent._teardown_task_branch = MagicMock(  # type: ignore[method-assign]
        side_effect=asyncio.CancelledError(),
    )

    with pytest.raises(asyncio.CancelledError):
        await teardown_task_run(
            agent, run_session=None, branch_name=None, kept_branch=None,
            preserve_branch=False, deferred=_deferred_record(),
            handed_off=True,
        )

    assert [t.task_id for t in agent.memory.get_recent_tasks(5)] == ["d1"]
