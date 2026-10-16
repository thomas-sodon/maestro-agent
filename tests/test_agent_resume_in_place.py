"""CLI-crash and stream-stall resume-in-place plus per-query usage aggregation;
split out of ``tests/test_agent.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.agent import (
    MaestroAgent,
)
from maestro.sdk import ErrorCategory, SDKError
from tests.test_agent import (
    _build_agent,
)

# --- CLI crash resume-in-place tests ---


@pytest.mark.asyncio
async def test_run_loop_survives_cli_crash(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() retries a CLI-crashed iteration in place, then continues."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    agent._iter_session_a_id = "ses_pre_crash"

    call_count = 0
    observed_ids_on_retry: list[str | None] = []

    async def contemplate_side_effect(**kwargs: Any) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise SDKError(
                "CLI exited with code 1",
                category=ErrorCategory.CLI_CRASH,
            )
        observed_ids_on_retry.append(agent._iter_session_a_id)
        return "ok"

    agent.contemplate = AsyncMock(side_effect=contemplate_side_effect)  # type: ignore[method-assign]

    wait_timeouts: list[float | None] = []

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        wait_timeouts.append(timeout)
        raise TimeoutError

    with patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    # 1 crash + 1 successful retry.
    assert call_count == 2
    # Persisted session id was preserved so the retry can resume the
    # server-side transcript instead of starting cold.
    assert observed_ids_on_retry == ["ses_pre_crash"]
    # No 300s cooldown — CLI crash is not capacity-driven.
    assert 300 not in wait_timeouts
    # Counter cleared after the clean iteration completed.
    assert agent._consecutive_cli_crashes == 0


@pytest.mark.asyncio
async def test_run_loop_cli_crash_journals_milestone(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() journals a milestone with triggered_by='cli_crash'."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    call_count = 0

    async def contemplate_side_effect(**kwargs: Any) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise SDKError(
                "CLI exited with code 1",
                category=ErrorCategory.CLI_CRASH,
            )
        return "ok"

    agent.contemplate = AsyncMock(side_effect=contemplate_side_effect)  # type: ignore[method-assign]

    journal_calls: list[dict[str, Any]] = []
    original_write = agent.journal.record_event

    def capture_write(**kwargs: Any) -> None:
        journal_calls.append(kwargs)
        original_write(**kwargs)

    agent.journal.record_event = capture_write  # type: ignore[method-assign]

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        raise TimeoutError

    with patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    crash_entries = [
        c for c in journal_calls if c.get("triggered_by") == "cli_crash"
    ]
    assert len(crash_entries) == 1
    assert crash_entries[0]["entry_type"] == "milestone"


@pytest.mark.asyncio
async def test_run_loop_cli_crash_journal_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() tolerates journal write failure during CLI-crash re-entry."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    call_count = 0

    async def contemplate_side_effect(**kwargs: Any) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise SDKError(
                "CLI exited with code 1",
                category=ErrorCategory.CLI_CRASH,
            )
        return "ok"

    agent.contemplate = AsyncMock(side_effect=contemplate_side_effect)  # type: ignore[method-assign]
    agent.journal.record_event = MagicMock(side_effect=OSError("disk full"))  # type: ignore[method-assign]

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        raise TimeoutError

    with patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    # Retry still happened despite the journal write failure.
    assert call_count == 2


@pytest.mark.asyncio
async def test_cli_crash_ceiling_crashes_loop(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Consecutive CLI crashes above the ceiling re-raise to the crash path."""
    from maestro.loop.iteration import _MAX_CONSECUTIVE_CLI_CRASHES

    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    agent.contemplate = AsyncMock(  # type: ignore[method-assign]
        side_effect=SDKError("cli crash", category=ErrorCategory.CLI_CRASH),
    )

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        raise TimeoutError

    with patch("asyncio.wait_for", side_effect=fake_wait_for), \
         pytest.raises(SDKError):
        await agent.run_loop(interval=0, max_iters=99, task_every=0, evolve_every=999)

    # The ceiling is _MAX_CONSECUTIVE_CLI_CRASHES — one past that re-raises.
    assert agent.contemplate.call_count == _MAX_CONSECUTIVE_CLI_CRASHES + 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_cli_crash_before_session_established(make_agent) -> None:  # type: ignore[no-untyped-def]
    """CLI crash with no prior session id leaves ids None and still retries."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    # No pre-existing session ids — the crash happens before any turn
    # successfully returned a session_id. The retry must still run and
    # leave ``_iter_session_a_id`` at None.
    assert agent._iter_session_a_id is None
    assert agent._iter_session_b_id is None

    call_count = 0
    persist_calls: list[tuple[int, str | None]] = []
    original_persist = agent._persist_progress

    def capture_persist(iteration: int, next_phase: str | None) -> None:
        persist_calls.append((iteration, next_phase))
        original_persist(iteration, next_phase)

    agent._persist_progress = capture_persist  # type: ignore[method-assign]

    async def contemplate_side_effect(**kwargs: Any) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise SDKError(
                "CLI exited with code 1",
                category=ErrorCategory.CLI_CRASH,
            )
        return "ok"

    agent.contemplate = AsyncMock(side_effect=contemplate_side_effect)  # type: ignore[method-assign]

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        raise TimeoutError

    with patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    # Retry completed the iteration.
    assert call_count == 2
    # Session ids remained None through the crash handler.
    assert agent._iter_session_a_id is None
    assert agent._iter_session_b_id is None
    # _persist_progress was called with the contemplate phase cursor at
    # least once (crash handler) — i.e. progress was written even though
    # no session was ever established.
    assert any(p == "contemplate" for _, p in persist_calls)


# --- Stream-stall resume-in-place tests ---


@pytest.mark.asyncio
async def test_stream_stall_clears_persisted_ids_and_retries(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A STREAM_STALL clears persisted ids (like STALE_SESSION) and retries."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    agent._iter_session_a_id = "ses_pre"
    agent._iter_session_b_id = "ses_pre_b"

    call_count = 0
    cleared_ids_observed: list[tuple[str | None, str | None]] = []

    async def contemplate_side_effect(**kwargs: Any) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise SDKError(
                "stream idle", category=ErrorCategory.STREAM_STALL,
            )
        cleared_ids_observed.append(
            (agent._iter_session_a_id, agent._iter_session_b_id),
        )
        return "ok"

    agent.contemplate = AsyncMock(side_effect=contemplate_side_effect)  # type: ignore[method-assign]

    wait_timeouts: list[float | None] = []

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        wait_timeouts.append(timeout)
        raise TimeoutError

    with patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    assert call_count == 2
    assert cleared_ids_observed == [(None, None)]
    assert 300 not in wait_timeouts
    # Counter is cleared on the clean iteration end.
    assert agent._consecutive_stream_stalls == 0


@pytest.mark.asyncio
async def test_stream_stall_ceiling_crashes_loop(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Consecutive stream stalls above the ceiling re-raise to the crash path."""
    from maestro.loop.iteration import _MAX_CONSECUTIVE_STREAM_STALLS

    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    agent.contemplate = AsyncMock(  # type: ignore[method-assign]
        side_effect=SDKError("stall", category=ErrorCategory.STREAM_STALL),
    )

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        raise TimeoutError

    with patch("asyncio.wait_for", side_effect=fake_wait_for), \
         pytest.raises(SDKError):
        await agent.run_loop(
            interval=0, max_iters=99, task_every=0, evolve_every=999,
        )

    assert agent.contemplate.call_count == _MAX_CONSECUTIVE_STREAM_STALLS + 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_stream_stall_journals_milestone(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() journals a milestone with triggered_by='stream_stall'."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    call_count = 0

    async def contemplate_side_effect(**kwargs: Any) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise SDKError(
                "stream idle", category=ErrorCategory.STREAM_STALL,
            )
        return "ok"

    agent.contemplate = AsyncMock(side_effect=contemplate_side_effect)  # type: ignore[method-assign]

    journal_calls: list[dict[str, Any]] = []
    original_write = agent.journal.record_event

    def capture_write(**kwargs: Any) -> None:
        journal_calls.append(kwargs)
        original_write(**kwargs)

    agent.journal.record_event = capture_write  # type: ignore[method-assign]

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        raise TimeoutError

    with patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    stall_entries = [
        c for c in journal_calls if c.get("triggered_by") == "stream_stall"
    ]
    assert len(stall_entries) == 1
    assert stall_entries[0]["entry_type"] == "milestone"


@pytest.mark.asyncio
async def test_stream_stall_journal_failure_is_swallowed(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Journal write failure during stream-stall recovery is logged, not raised."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    call_count = 0

    async def contemplate_side_effect(**kwargs: Any) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise SDKError(
                "stream idle", category=ErrorCategory.STREAM_STALL,
            )
        return "ok"

    agent.contemplate = AsyncMock(side_effect=contemplate_side_effect)  # type: ignore[method-assign]
    agent.journal.record_event = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("journal disk full"),
    )

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        raise TimeoutError

    with patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    # Loop completed without re-raising; retry path ran (contemplate called twice).
    assert call_count == 2


@pytest.mark.asyncio
async def test_stream_stall_resume_warning_includes_caller(  # type: ignore[no-untyped-def]
    make_agent, caplog,
) -> None:
    """The resume warning surfaces the caller tag from the failing SDKError
    so triage can identify which sub-call inside the phase stalled."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    call_count = 0

    async def contemplate_side_effect(**kwargs: Any) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise SDKError(
                "stream idle",
                category=ErrorCategory.STREAM_STALL,
                caller="task_generation_session",
            )
        return "ok"

    agent.contemplate = AsyncMock(side_effect=contemplate_side_effect)  # type: ignore[method-assign]

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        raise TimeoutError

    with patch("asyncio.wait_for", side_effect=fake_wait_for), \
         caplog.at_level("WARNING", logger="maestro"):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    stall_warnings = [
        r.getMessage() for r in caplog.records
        if "Stream stall at iteration" in r.getMessage()
    ]
    assert len(stall_warnings) == 1
    assert "caller=task_generation_session" in stall_warnings[0]


@pytest.mark.asyncio
async def test_stream_stall_resume_warning_uses_unknown_when_caller_missing(  # type: ignore[no-untyped-def]
    make_agent, caplog,
) -> None:
    """When the SDKError lacks a caller tag, the resume warning falls back
    to caller=unknown so the log line format is stable."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    call_count = 0

    async def contemplate_side_effect(**kwargs: Any) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise SDKError("stream idle", category=ErrorCategory.STREAM_STALL)
        return "ok"

    agent.contemplate = AsyncMock(side_effect=contemplate_side_effect)  # type: ignore[method-assign]

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        raise TimeoutError

    with patch("asyncio.wait_for", side_effect=fake_wait_for), \
         caplog.at_level("WARNING", logger="maestro"):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    stall_warnings = [
        r.getMessage() for r in caplog.records
        if "Stream stall at iteration" in r.getMessage()
    ]
    assert len(stall_warnings) == 1
    assert "caller=unknown" in stall_warnings[0]


# ---------------------------------------------------------------------------
# _on_query_usage — turn aggregation across one-shot calls and shared sessions
# ---------------------------------------------------------------------------


class TestOnQueryUsageTurnAggregation:
    """``_on_query_usage`` must keep ``per_caller_turns`` as a raw monotone
    sum (audit truth) and ``total_turns`` as a session-aware dedup that
    behaves correctly under both per-call and cumulative ``num_turns``
    semantics from the SDK."""

    def _seed_iteration(self, agent: MaestroAgent) -> None:
        from maestro.sdk import IterationUsage
        agent._iteration_usage = IterationUsage(
            iteration=1, started_at="2026-04-18T00:00:00+00:00",
        )

    def test_oneshot_calls_sum_directly(self, mem_fs: dict[str, str]) -> None:
        """Calls without a session_id sum into total_turns directly and
        match per_caller_turns for the single caller."""
        from maestro.sdk import QueryUsage
        agent = _build_agent(mem_fs)
        self._seed_iteration(agent)

        for nt in (1, 2, 3):
            agent._on_query_usage(
                QueryUsage(num_turns=nt, caller="x", session_id=None),
            )

        iu = agent._iteration_usage
        assert iu is not None
        assert iu.total_turns == 6
        assert iu.per_caller_turns == {"x": 6}

    def test_reused_session_cumulative_semantics_dedupes(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Three calls on the same session reporting cumulative
        ``num_turns`` (2, 5, 12) credit only the *new* turns above the
        prior peak — total_turns ends at the final peak (12). The raw
        per_caller_turns is the un-deduped sum (19) — the upper bound."""
        from maestro.sdk import QueryUsage
        agent = _build_agent(mem_fs)
        self._seed_iteration(agent)

        for nt in (2, 5, 12):
            agent._on_query_usage(
                QueryUsage(num_turns=nt, caller="x", session_id="s1"),
            )

        iu = agent._iteration_usage
        assert iu is not None
        assert iu.total_turns == 12
        assert iu.per_caller_turns == {"x": 19}

    def test_reused_session_per_call_semantics_documented_undercount(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Three calls on the same session each reporting ``num_turns=3``
        (per-call semantics, identical values) — total_turns dedupes to 3
        (under-reports the real work) while per_caller_turns preserves the
        raw 9. This documents the known caveat called out in CLAUDE.md."""
        from maestro.sdk import QueryUsage
        agent = _build_agent(mem_fs)
        self._seed_iteration(agent)

        for _ in range(3):
            agent._on_query_usage(
                QueryUsage(num_turns=3, caller="x", session_id="s1"),
            )

        iu = agent._iteration_usage
        assert iu is not None
        assert iu.total_turns == 3
        assert iu.per_caller_turns == {"x": 9}

    def test_no_iteration_usage_is_a_noop(self, mem_fs: dict[str, str]) -> None:
        """When ``_iteration_usage`` is unset (e.g. outside the loop), the
        sink must not raise."""
        from maestro.sdk import QueryUsage
        agent = _build_agent(mem_fs)
        agent._iteration_usage = None
        agent._on_query_usage(QueryUsage(num_turns=4, caller="x"))
