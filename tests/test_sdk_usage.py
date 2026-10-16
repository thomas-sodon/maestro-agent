"""Tests for per-iteration SDK usage persistence (sdk.py usage sink)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from maestro.sdk import (
    IterationUsage,
    QueryUsage,
    _log_sdk_call,
    clear_usage_sink,
    read_iteration_usage,
    set_usage_sink,
    write_iteration_usage,
)

if TYPE_CHECKING:
    from maestro.agent import MaestroAgent


class TestUsageSinkAccumulation:
    def test_sink_receives_usage(self) -> None:
        received: list[QueryUsage] = []
        set_usage_sink(received.append)

        u1 = QueryUsage(input_tokens=100, output_tokens=50, caller="a")
        u2 = QueryUsage(input_tokens=200, output_tokens=80, caller="b")
        u3 = QueryUsage(input_tokens=300, output_tokens=70, caller="a")

        for u in (u1, u2, u3):
            _log_sdk_call(u)

        assert len(received) == 3
        assert received[0].input_tokens == 100
        assert received[2].caller == "a"

    def test_sink_accumulates_iteration_usage(self) -> None:
        iu = IterationUsage(iteration=1, started_at="2026-01-01T00:00:00Z")

        def accumulate(usage: QueryUsage) -> None:
            iu.calls += 1
            iu.input_tokens += usage.input_tokens
            iu.output_tokens += usage.output_tokens
            iu.total_cost_usd += usage.total_cost_usd or 0.0
            caller = usage.caller or "unknown"
            iu.per_caller[caller] = iu.per_caller.get(caller, 0) + 1

        set_usage_sink(accumulate)

        _log_sdk_call(QueryUsage(input_tokens=100, output_tokens=50, total_cost_usd=0.05, caller="contemplate"))
        _log_sdk_call(QueryUsage(input_tokens=200, output_tokens=80, total_cost_usd=0.10, caller="execute_step"))
        _log_sdk_call(QueryUsage(input_tokens=150, output_tokens=60, total_cost_usd=0.08, caller="contemplate"))

        assert iu.calls == 3
        assert iu.input_tokens == 450
        assert iu.output_tokens == 190
        assert abs(iu.total_cost_usd - 0.23) < 1e-9
        assert iu.per_caller == {"contemplate": 2, "execute_step": 1}

    def test_sink_exception_safety(self) -> None:
        def bad_sink(usage: QueryUsage) -> None:
            raise RuntimeError("boom")

        set_usage_sink(bad_sink)
        _log_sdk_call(QueryUsage(input_tokens=100, output_tokens=50))

    def test_clear_usage_sink(self) -> None:
        received: list[QueryUsage] = []
        set_usage_sink(received.append)

        _log_sdk_call(QueryUsage(input_tokens=100, output_tokens=50))
        assert len(received) == 1

        clear_usage_sink()
        _log_sdk_call(QueryUsage(input_tokens=200, output_tokens=80))
        assert len(received) == 1

    def test_no_sink_registered(self) -> None:
        _log_sdk_call(QueryUsage(input_tokens=100, output_tokens=50))


class TestIterationUsageRoundTrip:
    def test_write_and_read(self, mem_fs: dict[str, str]) -> None:
        path = Path("/tmp/usage.jsonl")
        for i in range(3):
            write_iteration_usage(path, IterationUsage(
                iteration=i + 1,
                started_at=f"2026-01-0{i + 1}T00:00:00Z",
                calls=i + 2,
                input_tokens=(i + 1) * 100,
                output_tokens=(i + 1) * 50,
                total_cost_usd=(i + 1) * 0.05,
                per_caller={"test": i + 1},
            ))

        results = read_iteration_usage(path, n=2)
        assert len(results) == 2
        assert results[0].iteration == 2
        assert results[1].iteration == 3
        assert results[1].calls == 4
        assert results[1].per_caller == {"test": 3}

    def test_bounded_reader(self, mem_fs: dict[str, str]) -> None:
        path = Path("/tmp/usage_big.jsonl")
        for i in range(100):
            write_iteration_usage(path, IterationUsage(
                iteration=i, started_at="2026-01-01T00:00:00Z",
            ))

        results = read_iteration_usage(path, n=10)
        assert len(results) == 10
        assert results[0].iteration == 90
        assert results[9].iteration == 99

    def test_missing_file_returns_empty(self, mem_fs: dict[str, str]) -> None:
        results = read_iteration_usage(Path("/tmp/nonexistent.jsonl"))
        assert results == []

    def test_malformed_lines_skipped(self, mem_fs: dict[str, str]) -> None:
        path = Path("/tmp/bad.jsonl")
        content = '{"iteration": 1, "started_at": "t"}\nnot-json\n{"iteration": 2, "started_at": "t"}\n'
        mem_fs[str(path)] = content

        results = read_iteration_usage(path)
        assert len(results) == 2
        assert results[0].iteration == 1
        assert results[1].iteration == 2

    @pytest.mark.parametrize("line", ["null", "[]", "[1, 2]", "3", '"text"', "true"])
    def test_non_object_lines_skipped(self, mem_fs: dict[str, str], line: str) -> None:
        path = Path("/tmp/non_object.jsonl")
        mem_fs[str(path)] = (
            '{"iteration": 1, "started_at": "t"}\n'
            f"{line}\n"
            '{"iteration": 2, "started_at": "t"}\n'
        )

        results = read_iteration_usage(path)
        assert len(results) == 2
        assert results[0].iteration == 1
        assert results[1].iteration == 2

    def test_empty_lines_skipped(self, mem_fs: dict[str, str]) -> None:
        path = Path("/tmp/empty_lines.jsonl")
        content = '{"iteration": 1, "started_at": "t"}\n\n\n{"iteration": 2, "started_at": "t"}\n'
        mem_fs[str(path)] = content

        results = read_iteration_usage(path)
        assert len(results) == 2

    def test_oserror_on_open_returns_empty(self, mem_fs: dict[str, str]) -> None:
        from unittest.mock import patch

        path = Path("/tmp/exists_but_broken.jsonl")
        mem_fs[str(path)] = "data"

        with patch("builtins.open", side_effect=OSError("disk error")):
            results = read_iteration_usage(path)
        assert results == []

    def test_absent_total_cost_usd_defaults_to_zero(
        self, mem_fs: dict[str, str],
    ) -> None:
        """A usage.jsonl record missing ``total_cost_usd`` must not raise.

        Guards the ``d.get("total_cost_usd", 0.0)`` fallback in
        :func:`read_iteration_usage`. Prior to that fix, records written by
        an older schema (without the cost field) raised ``KeyError`` on read.
        This test pre-populates mem_fs with a record that lacks the key and
        asserts the reader returns a record with ``total_cost_usd == 0.0``.
        """
        path = Path("/tmp/no_cost.jsonl")
        # Hand-crafted JSONL record with every field EXCEPT total_cost_usd.
        content = (
            '{"iteration": 7, "started_at": "2026-04-16T00:00:00Z", '
            '"calls": 3, "input_tokens": 400, "output_tokens": 200, '
            '"cache_read_tokens": 120, "cache_creation_tokens": 40, '
            '"per_caller": {"contemplate": 3}}\n'
        )
        mem_fs[str(path)] = content

        # Must not raise KeyError.
        results = read_iteration_usage(path)

        assert len(results) == 1
        r = results[0]
        # Other fields should still deserialize normally.
        assert r.iteration == 7
        assert r.started_at == "2026-04-16T00:00:00Z"
        assert r.calls == 3
        assert r.input_tokens == 400
        assert r.output_tokens == 200
        assert r.cache_read_tokens == 120
        assert r.cache_creation_tokens == 40
        assert r.per_caller == {"contemplate": 3}
        # The missing field must fall back to the safe default.
        assert r.total_cost_usd == 0.0

    def test_absent_total_cost_usd_mixed_with_valid_records(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Records with and without ``total_cost_usd`` coexist safely."""
        path = Path("/tmp/mixed_cost.jsonl")
        content = (
            '{"iteration": 1, "started_at": "t1", "total_cost_usd": 0.25}\n'
            '{"iteration": 2, "started_at": "t2"}\n'
            '{"iteration": 3, "started_at": "t3", "total_cost_usd": 0.5}\n'
        )
        mem_fs[str(path)] = content

        results = read_iteration_usage(path)

        assert len(results) == 3
        assert abs(results[0].total_cost_usd - 0.25) < 1e-9
        # Missing field falls back to 0.0 — no KeyError.
        assert results[1].total_cost_usd == 0.0
        assert abs(results[2].total_cost_usd - 0.5) < 1e-9

    def test_all_fields_round_trip(self, mem_fs: dict[str, str]) -> None:
        path = Path("/tmp/full.jsonl")
        usage = IterationUsage(
            iteration=42,
            started_at="2026-04-14T12:00:00Z",
            calls=5,
            input_tokens=1000,
            output_tokens=500,
            cache_read_tokens=800,
            cache_creation_tokens=200,
            total_cost_usd=0.42,
            per_caller={"contemplate": 2, "execute_step": 3},
        )
        write_iteration_usage(path, usage)

        results = read_iteration_usage(path)
        assert len(results) == 1
        r = results[0]
        assert r.iteration == 42
        assert r.started_at == "2026-04-14T12:00:00Z"
        assert r.calls == 5
        assert r.input_tokens == 1000
        assert r.output_tokens == 500
        assert r.cache_read_tokens == 800
        assert r.cache_creation_tokens == 200
        assert abs(r.total_cost_usd - 0.42) < 1e-9
        assert r.per_caller == {"contemplate": 2, "execute_step": 3}
        assert r.task_id is None

    def test_task_id_round_trip(self, mem_fs: dict[str, str]) -> None:
        path = Path("/tmp/task_id.jsonl")
        usage = IterationUsage(
            iteration=10,
            started_at="2026-05-12T00:00:00Z",
            calls=1,
            task_id="abc-123",
        )
        write_iteration_usage(path, usage)

        results = read_iteration_usage(path)
        assert len(results) == 1
        assert results[0].task_id == "abc-123"

    def test_task_id_none_round_trip(self, mem_fs: dict[str, str]) -> None:
        path = Path("/tmp/task_id_none.jsonl")
        usage = IterationUsage(
            iteration=10,
            started_at="2026-05-12T00:00:00Z",
        )
        write_iteration_usage(path, usage)

        results = read_iteration_usage(path)
        assert len(results) == 1
        assert results[0].task_id is None

    def test_task_id_in_to_dict(self) -> None:
        usage = IterationUsage(
            iteration=1, started_at="t", task_id="my-task",
        )
        d = usage.to_dict()
        assert d["task_id"] == "my-task"

    def test_task_id_none_in_to_dict(self) -> None:
        usage = IterationUsage(iteration=1, started_at="t")
        d = usage.to_dict()
        assert d["task_id"] is None

    def test_task_id_absent_from_dict_defaults_none(self) -> None:
        d = {"iteration": 5, "started_at": "t"}
        usage = IterationUsage.from_dict(d)
        assert usage.task_id is None

    def test_task_id_non_string_from_dict_defaults_none(self) -> None:
        d = {"iteration": 5, "started_at": "t", "task_id": 42}
        usage = IterationUsage.from_dict(d)
        assert usage.task_id is None

    def test_old_row_missing_task_id_round_trips_via_jsonl(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Pre-task_id JSONL rows deserialize with task_id=None.

        Simulates a usage.jsonl file written before the ``task_id`` field
        existed.  ``read_iteration_usage`` must not raise and must default
        the missing key to ``None``.
        """
        path = Path("/tmp/old_schema.jsonl")
        # Hand-crafted JSONL without the ``task_id`` key.
        mem_fs[str(path)] = (
            '{"iteration": 3, "started_at": "2026-04-01T00:00:00Z", '
            '"calls": 2, "input_tokens": 500, "output_tokens": 250, '
            '"cache_read_tokens": 100, "cache_creation_tokens": 50, '
            '"total_cost_usd": 0.1, "per_caller": {"contemplate": 2}, '
            '"total_turns": 4, "per_caller_turns": {"contemplate": 4}}\n'
        )

        results = read_iteration_usage(path)
        assert len(results) == 1
        r = results[0]
        assert r.iteration == 3
        assert r.calls == 2
        assert r.input_tokens == 500
        assert r.total_cost_usd == 0.1
        assert r.total_turns == 4
        # The missing field must default to None — no KeyError.
        assert r.task_id is None

    def test_mixed_old_and_new_rows_coexist(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Old rows (no task_id) and new rows (with task_id) coexist safely."""
        path = Path("/tmp/mixed_schema.jsonl")
        mem_fs[str(path)] = (
            # Old row — no task_id key at all.
            '{"iteration": 1, "started_at": "t1", "calls": 1}\n'
            # New row — task_id present.
            '{"iteration": 2, "started_at": "t2", "calls": 2, '
            '"task_id": "abc123def456"}\n'
            # New row — task_id explicitly null.
            '{"iteration": 3, "started_at": "t3", "calls": 0, '
            '"task_id": null}\n'
        )

        results = read_iteration_usage(path)
        assert len(results) == 3
        assert results[0].task_id is None         # old row
        assert results[1].task_id == "abc123def456"  # new row with id
        assert results[2].task_id is None          # new row, no task

    def test_unmetered_round_trips(self, mem_fs: dict[str, str]) -> None:
        """An unmetered IterationUsage row round-trips through usage.jsonl."""
        path = Path("/tmp/unmetered.jsonl")
        write_iteration_usage(path, IterationUsage(
            iteration=8, started_at="2026-05-30T00:00:00Z", calls=2,
            unmetered=True,
        ))
        results = read_iteration_usage(path)
        assert len(results) == 1
        assert results[0].unmetered is True

    def test_unmetered_in_to_dict(self) -> None:
        d = IterationUsage(iteration=1, started_at="t", unmetered=True).to_dict()
        assert d["unmetered"] is True

    def test_unmetered_defaults_false_in_to_dict(self) -> None:
        d = IterationUsage(iteration=1, started_at="t").to_dict()
        assert d["unmetered"] is False

    def test_unmetered_absent_from_dict_defaults_false(self) -> None:
        """A pre-Step-13 row without the key deserializes as metered."""
        usage = IterationUsage.from_dict({"iteration": 5, "started_at": "t"})
        assert usage.unmetered is False

    def test_providers_round_trip(self, mem_fs: dict[str, str]) -> None:
        """Provider attribution (067 Step 2) round-trips through usage.jsonl."""
        path = Path("/tmp/providers.jsonl")
        write_iteration_usage(path, IterationUsage(
            iteration=9, started_at="2026-06-12T00:00:00Z", calls=2,
            providers=["anthropic", "cli_paced"],
        ))
        results = read_iteration_usage(path)
        assert len(results) == 1
        assert results[0].providers == ["anthropic", "cli_paced"]

    def test_providers_in_to_dict_defaults_empty(self) -> None:
        d = IterationUsage(iteration=1, started_at="t").to_dict()
        assert d["providers"] == []

    def test_providers_absent_from_dict_defaults_empty(self) -> None:
        """A pre-067 row without the key deserializes with no attribution."""
        usage = IterationUsage.from_dict({"iteration": 5, "started_at": "t"})
        assert usage.providers == []

    def test_providers_non_list_from_dict_defaults_empty(self) -> None:
        usage = IterationUsage.from_dict(
            {"iteration": 5, "started_at": "t", "providers": "anthropic"}
        )
        assert usage.providers == []

    def test_providers_non_string_entries_dropped(self) -> None:
        usage = IterationUsage.from_dict(
            {"iteration": 5, "started_at": "t", "providers": ["anthropic", 7]}
        )
        assert usage.providers == ["anthropic"]


class TestAgentOnQueryUsage:
    """Tests for MaestroAgent._on_query_usage accumulation."""

    def test_accumulates_usage(self, make_agent) -> None:  # type: ignore[no-untyped-def]
        agent: MaestroAgent = make_agent()
        agent._iteration_usage = IterationUsage(
            iteration=1, started_at="2026-01-01T00:00:00Z",
        )

        agent._on_query_usage(QueryUsage(
            input_tokens=100, output_tokens=50,
            cache_read_tokens=80, cache_creation_tokens=20,
            total_cost_usd=0.05, caller="contemplate",
        ))
        agent._on_query_usage(QueryUsage(
            input_tokens=200, output_tokens=70,
            cache_read_tokens=150, cache_creation_tokens=10,
            total_cost_usd=0.10, caller="execute_step",
        ))

        iu = agent._iteration_usage
        assert iu.calls == 2
        assert iu.input_tokens == 300
        assert iu.output_tokens == 120
        assert iu.cache_read_tokens == 230
        assert iu.cache_creation_tokens == 30
        assert abs(iu.total_cost_usd - 0.15) < 1e-9
        assert iu.per_caller == {"contemplate": 1, "execute_step": 1}

    def test_early_exit_turn_credited_then_dedupes_on_session(self, make_agent) -> None:  # type: ignore[no-untyped-def]
        """An early-exit turn (#1288) credits one turn, and a later cumulative
        ResultMessage call on the same session credits only the delta."""
        agent: MaestroAgent = make_agent()
        agent._iteration_usage = IterationUsage(iteration=1, started_at="t")

        # Early-exit-shaped row as _finalize_usage actually emits it: the floor
        # stamped num_turns=1 and the branch propagated cr.last_session_id onto
        # the usage row (#1288), so crediting goes through the session-aware
        # branch and establishes a per-session peak of 1.
        agent._on_query_usage(QueryUsage(
            num_turns=1, session_id="s1", caller="introspect",
        ))
        iu = agent._iteration_usage
        assert iu.per_caller_turns["introspect"] == 1
        assert iu.total_turns == 1
        assert iu._session_peak_turns["s1"] == 1

        # A later real ResultMessage call on the same session reports the
        # cumulative turn count (3). The peak dedupe credits only the new
        # delta (3 - 1 = 2) to total_turns — no double count, no drop.
        agent._on_query_usage(QueryUsage(
            num_turns=3, session_id="s1", caller="introspect",
        ))
        assert iu.total_turns == 3
        assert iu._session_peak_turns["s1"] == 3
        # per_caller_turns sums directly (an upper bound under cumulative
        # semantics for a caller reusing a session): 1 + 3 = 4.
        assert iu.per_caller_turns["introspect"] == 4

    def test_no_op_when_no_iteration_usage(self, make_agent) -> None:  # type: ignore[no-untyped-def]
        agent: MaestroAgent = make_agent()
        assert agent._iteration_usage is None
        agent._on_query_usage(QueryUsage(input_tokens=100, output_tokens=50))

    def test_none_cost_treated_as_zero(self, make_agent) -> None:  # type: ignore[no-untyped-def]
        agent: MaestroAgent = make_agent()
        agent._iteration_usage = IterationUsage(
            iteration=1, started_at="t",
        )
        agent._on_query_usage(QueryUsage(
            input_tokens=100, output_tokens=50, total_cost_usd=None,
        ))
        assert agent._iteration_usage.total_cost_usd == 0.0

    def test_metered_only_iteration_stays_metered(self, make_agent) -> None:  # type: ignore[no-untyped-def]
        agent: MaestroAgent = make_agent()
        agent._iteration_usage = IterationUsage(iteration=1, started_at="t")
        agent._on_query_usage(QueryUsage(total_cost_usd=0.05, caller="a"))
        assert agent._iteration_usage.unmetered is False

    def test_unmetered_call_flags_iteration(self, make_agent) -> None:  # type: ignore[no-untyped-def]
        """A single unmetered call marks the whole iteration unmetered, so its
        cost total is treated as a lower bound rather than a measurement."""
        agent: MaestroAgent = make_agent()
        agent._iteration_usage = IterationUsage(iteration=1, started_at="t")
        # First a metered call, then an unmetered one — the flag latches True.
        agent._on_query_usage(QueryUsage(total_cost_usd=0.05, caller="a"))
        agent._on_query_usage(QueryUsage(
            total_cost_usd=None, caller="cli_phase", unmetered=True,
        ))
        assert agent._iteration_usage.unmetered is True
        assert agent._iteration_usage.calls == 2

    def test_provider_attribution_accumulates_distinct(self, make_agent) -> None:  # type: ignore[no-untyped-def]
        """Each producing provider is recorded once, in first-seen order (067)."""
        agent: MaestroAgent = make_agent()
        agent._iteration_usage = IterationUsage(iteration=1, started_at="t")
        agent._on_query_usage(QueryUsage(caller="a", provider="cli_paced"))
        agent._on_query_usage(QueryUsage(caller="b", provider="anthropic"))
        agent._on_query_usage(QueryUsage(caller="c", provider="cli_paced"))
        assert agent._iteration_usage.providers == ["cli_paced", "anthropic"]

    def test_provider_none_not_recorded(self, make_agent) -> None:  # type: ignore[no-untyped-def]
        """A stamp-less call (pre-067 shape) adds no attribution entry."""
        agent: MaestroAgent = make_agent()
        agent._iteration_usage = IterationUsage(iteration=1, started_at="t")
        agent._on_query_usage(QueryUsage(caller="a"))
        assert agent._iteration_usage.providers == []

    @pytest.mark.asyncio
    async def test_run_loop_flushes_usage_and_survives_oserror(
        self, make_agent, mem_fs: dict[str, str],
    ) -> None:  # type: ignore[no-untyped-def]
        """run_loop() flushes iteration usage; OSError on write is swallowed."""
        from unittest.mock import AsyncMock, MagicMock

        agent: MaestroAgent = make_agent()
        agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
        agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
        agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
        agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
        agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
        agent.evolve = AsyncMock()  # type: ignore[method-assign]
        agent.shutdown = AsyncMock()  # type: ignore[method-assign]

        with patch(
            "maestro.agent.write_iteration_usage",
            side_effect=OSError("disk full"),
        ):
            result = await agent.run_loop(
                interval=0, max_iters=1, task_every=0, evolve_every=999,
            )

        assert result is True
        assert agent._iteration_usage is None

    @staticmethod
    def _stop_during(agent: MaestroAgent, phase: str) -> None:
        """Make ``phase`` book usage for a task, then request shutdown."""
        from unittest.mock import AsyncMock, MagicMock

        agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
        agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
        agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
        agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
        agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
        agent.evolve = AsyncMock()  # type: ignore[method-assign]

        async def _book_and_stop(*, iteration: int) -> None:
            usage = agent._iteration_usage
            assert usage is not None
            usage.calls = 3
            usage.task_id = "task-4149"
            agent._ensure_shutdown_event().set()

        agent._phase_runners[phase] = _book_and_stop

    @pytest.mark.asyncio
    async def test_graceful_shutdown_mid_iteration_preserves_usage_in_lifecycle(
        self, make_agent, mem_fs: dict[str, str],
    ) -> None:  # type: ignore[no-untyped-def]
        """A graceful stop mid-iteration mirrors the accumulator (#4149)."""
        import json

        agent: MaestroAgent = make_agent()
        self._stop_during(agent, "ensure_main_branch")

        result = await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

        assert result is True
        data = json.loads(
            agent.config.lifecycle_path.read_text(encoding="utf-8"),
        )
        assert data["next_phase"] == "check_contributions"
        usage = data["iteration_usage"]
        assert usage["iteration"] == 1
        assert usage["calls"] == 3
        assert usage["task_id"] == "task-4149"
        assert read_iteration_usage(agent.config.usage_log_path) == []
        assert agent._iteration_usage is None

    @pytest.mark.asyncio
    async def test_graceful_shutdown_after_last_phase_flushes_usage_row(
        self, make_agent, mem_fs: dict[str, str],
    ) -> None:  # type: ignore[no-untyped-def]
        """A graceful stop after the final phase books one usage row (#4149)."""
        import json

        agent: MaestroAgent = make_agent()
        self._stop_during(agent, "dream")

        result = await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
            dream_every=1,
        )

        assert result is True
        records = read_iteration_usage(agent.config.usage_log_path)
        assert len(records) == 1
        assert records[0].iteration == 1
        assert records[0].task_id == "task-4149"
        data = json.loads(
            agent.config.lifecycle_path.read_text(encoding="utf-8"),
        )
        assert data["iteration_usage"] is None

    @pytest.mark.asyncio
    async def test_run_loop_writes_usage_for_two_iterations(
        self, make_agent, mem_fs: dict[str, str],
    ) -> None:  # type: ignore[no-untyped-def]
        """run_loop(max_iters=2) writes 2 lines to usage.jsonl and resets accumulator."""
        from unittest.mock import AsyncMock, MagicMock

        agent: MaestroAgent = make_agent()
        agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
        agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
        agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
        agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
        agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
        agent.evolve = AsyncMock()  # type: ignore[method-assign]
        agent.shutdown = AsyncMock()  # type: ignore[method-assign]

        result = await agent.run_loop(
            interval=0, max_iters=2, task_every=0, evolve_every=999,
        )

        assert result is True
        assert agent._iteration_usage is None

        records = read_iteration_usage(agent.config.usage_log_path)
        assert len(records) == 2
        assert records[0].iteration == 1
        assert records[1].iteration == 2

    @pytest.mark.asyncio
    async def test_run_loop_clears_sink_in_finally(
        self, make_agent, mem_fs: dict[str, str],
    ) -> None:  # type: ignore[no-untyped-def]
        """clear_usage_sink() is called even when run_loop exits normally."""
        from unittest.mock import AsyncMock, MagicMock

        agent: MaestroAgent = make_agent()
        agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
        agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
        agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
        agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
        agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
        agent.evolve = AsyncMock()  # type: ignore[method-assign]
        agent.shutdown = AsyncMock()  # type: ignore[method-assign]

        with patch("maestro.loop.iteration.clear_usage_sink") as mock_clear:
            await agent.run_loop(
                interval=0, max_iters=1, task_every=0, evolve_every=999,
            )

        mock_clear.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_stamps_task_id_on_iteration_usage(
        self, make_agent, mem_fs: dict[str, str],
    ) -> None:  # type: ignore[no-untyped-def]
        """run() sets _iteration_usage.task_id when an accumulator exists."""
        from unittest.mock import AsyncMock

        agent: MaestroAgent = make_agent()
        agent._iteration_usage = IterationUsage(
            iteration=1, started_at="2026-01-01T00:00:00Z",
        )
        assert agent._iteration_usage.task_id is None

        # Stub out planner so run() returns quickly (empty plan = failure).
        with patch.object(
            agent.planner, "decompose", AsyncMock(return_value=[]),
        ), patch("maestro.agent.query", new_callable=AsyncMock):
            record = await agent.run("test goal")

        # task_id should have been stamped — a 12-char hex string.
        assert len(record.task_id) == 12
        # run() does not clear _iteration_usage, so verify directly.
        assert agent._iteration_usage is not None
        assert agent._iteration_usage.task_id == record.task_id
        # Also verify via the serialized dict.
        d = agent._iteration_usage.to_dict()
        assert d["task_id"] == record.task_id

    @pytest.mark.asyncio
    async def test_run_without_iteration_usage_does_not_crash(
        self, make_agent, mem_fs: dict[str, str],
    ) -> None:  # type: ignore[no-untyped-def]
        """run() tolerates _iteration_usage=None (e.g. standalone run)."""
        from unittest.mock import AsyncMock

        agent: MaestroAgent = make_agent()
        assert agent._iteration_usage is None

        with patch.object(
            agent.planner, "decompose", AsyncMock(return_value=[]),
        ), patch("maestro.agent.query", new_callable=AsyncMock):
            await agent.run("test goal")
        # Should complete without error.

    @pytest.mark.asyncio
    async def test_flush_writes_task_id_to_jsonl(
        self, make_agent, mem_fs: dict[str, str],
    ) -> None:  # type: ignore[no-untyped-def]
        """write_iteration_usage persists task_id; read_iteration_usage recovers it."""
        agent: MaestroAgent = make_agent()
        usage = IterationUsage(
            iteration=5, started_at="2026-05-12T00:00:00Z",
            calls=3, task_id="deadbeef0123",
        )
        write_iteration_usage(agent.config.usage_log_path, usage)

        records = read_iteration_usage(agent.config.usage_log_path)
        assert len(records) == 1
        assert records[0].task_id == "deadbeef0123"

    @pytest.mark.asyncio
    async def test_flush_writes_none_task_id_to_jsonl(
        self, make_agent, mem_fs: dict[str, str],
    ) -> None:  # type: ignore[no-untyped-def]
        """Iterations without a task flush task_id=null and read back as None."""
        agent: MaestroAgent = make_agent()
        usage = IterationUsage(
            iteration=6, started_at="2026-05-12T00:00:00Z", calls=1,
        )
        write_iteration_usage(agent.config.usage_log_path, usage)

        records = read_iteration_usage(agent.config.usage_log_path)
        assert len(records) == 1
        assert records[0].task_id is None


def _cost_result(total: float | None, session_id: str = "s1") -> Any:
    """A ``ResultMessage`` reporting the CLI's running *total* cost."""
    from claude_agent_sdk.types import ResultMessage
    return ResultMessage(
        subtype="result", duration_ms=10, duration_api_ms=8, is_error=False,
        num_turns=1, session_id=session_id, result="ok", total_cost_usd=total,
    )


def _session_client(*turns: list[Any]) -> Any:
    """A mocked session client replaying one message list per turn."""
    from unittest.mock import AsyncMock, MagicMock
    client = MagicMock()
    client.query = AsyncMock()
    client.interrupt = AsyncMock()
    queue = [list(t) for t in turns]

    def receive() -> Any:
        batch = queue.pop(0) if queue else []

        async def _gen() -> Any:
            for msg in batch:
                yield msg
        return _gen()

    client.receive_messages = receive
    return client


class TestSessionCumulativeCost:
    """#4306 — a session's running ``total_cost_usd`` is booked as per-turn
    deltas, so k turns on one CLI process book k turns of spend, not k(k+1)/2."""

    @pytest.fixture(autouse=True)
    def _backend(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        from unittest.mock import MagicMock

        import maestro.sdk as sdk_mod
        backend = MagicMock()
        monkeypatch.setattr(sdk_mod, "_cost_sample_backend", backend)
        monkeypatch.setattr(sdk_mod, "_warning_throttle_multiplier", 1)
        return backend

    async def _run(self, client: Any, n: int, **kw: Any) -> list[float | None]:
        from maestro.sdk import query_on_session
        out: list[float | None] = []
        with patch("maestro.sdk.asyncio.sleep"):
            for _ in range(n):
                res = await query_on_session(client, "p", caller="t", **kw)
                out.append(res.usage.total_cost_usd)
        return out

    @pytest.mark.asyncio
    async def test_running_totals_book_as_deltas(self, _backend: Any) -> None:
        client = _session_client(
            [_cost_result(0.10)], [_cost_result(0.25)], [_cost_result(0.45)],
        )
        costs = await self._run(client, 3)
        assert costs == pytest.approx([0.10, 0.15, 0.20])
        booked = sum(c.args[0].cost_usd for c in _backend.append.call_args_list)
        assert booked == pytest.approx(0.45)

    @pytest.mark.asyncio
    async def test_distinct_client_starts_at_own_total(self) -> None:
        first = _session_client([_cost_result(0.30)])
        second = _session_client([_cost_result(0.05)])
        assert await self._run(first, 1) == pytest.approx([0.30])
        assert await self._run(second, 1) == pytest.approx([0.05])

    @pytest.mark.asyncio
    async def test_conversation_reset_rebases(self) -> None:
        from claude_agent_sdk.types import ConversationResetMessage
        reset = ConversationResetMessage(
            new_conversation_id="c2", uuid="u", session_id="s1",
        )
        client = _session_client(
            [_cost_result(0.40)], [reset, _cost_result(0.07)],
        )
        assert await self._run(client, 2) == pytest.approx([0.40, 0.07])

    @pytest.mark.asyncio
    async def test_total_below_baseline_books_zero(self) -> None:
        client = _session_client([_cost_result(0.50)], [_cost_result(0.20)])
        costs = await self._run(client, 2)
        assert costs == pytest.approx([0.50, 0.0])

    @pytest.mark.asyncio
    async def test_missing_total_keeps_baseline(self) -> None:
        client = _session_client(
            [_cost_result(0.10)], [_cost_result(None)], [_cost_result(0.30)],
        )
        costs = await self._run(client, 3)
        assert costs[0] == pytest.approx(0.10)
        assert costs[1] is None
        assert costs[2] == pytest.approx(0.20)

    @pytest.mark.asyncio
    async def test_early_exit_drained_result_advances_baseline(self) -> None:
        from claude_agent_sdk.types import AssistantMessage, TextBlock
        text = AssistantMessage(content=[TextBlock(text="hi")], model="m")
        client = _session_client(
            [text], [_cost_result(0.20)], [_cost_result(0.35)],
        )
        from maestro.sdk import query_on_session
        with patch("maestro.sdk.asyncio.sleep"):
            await query_on_session(
                client, "p", caller="t", complete_on=lambda _m, buf: bool(buf),
            )
            nxt = await query_on_session(client, "p", caller="t")
        assert nxt.usage.total_cost_usd == pytest.approx(0.15)

    @pytest.mark.asyncio
    async def test_early_exit_undrained_result_keeps_baseline(self) -> None:
        """An interrupted turn whose ResultMessage never drains leaves the
        baseline put: its real cost lands in the next turn's delta."""
        from claude_agent_sdk.types import AssistantMessage, TextBlock
        text = AssistantMessage(content=[TextBlock(text="hi")], model="m")
        client = _session_client([_cost_result(0.10)], [text], [], [_cost_result(0.35)])
        from maestro.sdk import query_on_session
        with patch("maestro.sdk.asyncio.sleep"):
            await query_on_session(client, "p", caller="t")
            await query_on_session(
                client, "p", caller="t", complete_on=lambda _m, buf: bool(buf),
            )
            nxt = await query_on_session(client, "p", caller="t")
        assert nxt.usage.total_cost_usd == pytest.approx(0.25)

    @pytest.mark.asyncio
    async def test_reset_without_result_zeroes_baseline(self) -> None:
        from claude_agent_sdk.types import (
            AssistantMessage,
            ConversationResetMessage,
            TextBlock,
        )
        reset = ConversationResetMessage(
            new_conversation_id="c2", uuid="u", session_id="s1",
        )
        text = AssistantMessage(content=[TextBlock(text="hi")], model="m")
        client = _session_client(
            [_cost_result(0.40)], [reset, text], [], [_cost_result(0.06)],
        )
        from maestro.sdk import query_on_session
        with patch("maestro.sdk.asyncio.sleep"):
            await query_on_session(client, "p", caller="t")
            await query_on_session(
                client, "p", caller="t", complete_on=lambda _m, buf: bool(buf),
            )
            nxt = await query_on_session(client, "p", caller="t")
        assert nxt.usage.total_cost_usd == pytest.approx(0.06)

    @pytest.mark.asyncio
    async def test_oneshot_query_books_total_unchanged(self) -> None:
        from maestro.sdk import query

        async def gen(**_kw: Any) -> Any:
            yield _cost_result(0.45)

        with patch("maestro.sdk._query", side_effect=gen), patch(
            "maestro.sdk.asyncio.sleep",
        ):
            res = await query("p")
        assert res.usage.total_cost_usd == pytest.approx(0.45)


def _billed_error_result(
    total: float, *, subtype: str = "error_max_structured_output_retries",
    is_error: bool = True, result: str = "",
) -> Any:
    """A billed terminal ``ResultMessage`` whose consume step raises."""
    from claude_agent_sdk.types import ResultMessage
    return ResultMessage(
        subtype=subtype, duration_ms=10, duration_api_ms=8, is_error=is_error,
        num_turns=1, session_id="s1", result=result, total_cost_usd=total,
        usage={"input_tokens": 5000, "output_tokens": 3000},
    )


_SCHEMA = {"type": "json_schema", "schema": {"type": "object"}}


class TestBilledErrorResultIsMetered:
    """#4572 — a ResultMessage received before a categorized ``SDKError``
    raise is booked exactly once, and a session's baseline advances past it."""

    @pytest.fixture(autouse=True)
    def _backend(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        from unittest.mock import MagicMock

        import maestro.sdk as sdk_mod
        backend = MagicMock()
        sink = MagicMock()
        monkeypatch.setattr(sdk_mod, "_cost_sample_backend", backend)
        monkeypatch.setattr(sdk_mod, "_usage_sink", sink)
        monkeypatch.setattr(sdk_mod, "_warning_throttle_multiplier", 1)
        self.sink = sink
        return backend

    def _booked(self, backend: Any) -> list[float]:
        return [c.args[0].cost_usd for c in backend.append.call_args_list]

    async def _stateless(self, msg: Any, **opts: Any) -> Any:
        from maestro.sdk import QueryOptions, SDKError, query

        async def gen(**_kw: Any) -> Any:
            yield msg

        with patch("maestro.sdk._query", side_effect=gen), patch(
            "maestro.sdk.asyncio.sleep",
        ), pytest.raises(SDKError) as exc:
            await query(
                "p", options=QueryOptions(caller="scout_opportunities", **opts),
            )
        return exc.value

    async def _session_turn(self, client: Any, **kw: Any) -> Any:
        from maestro.sdk import query_on_session
        with patch("maestro.sdk.asyncio.sleep"):
            return await query_on_session(client, "p", caller="t", **kw)

    async def _session_error(self, client: Any, **kw: Any) -> Any:
        from maestro.sdk import SDKError
        with pytest.raises(SDKError) as exc:
            await self._session_turn(client, **kw)
        return exc.value

    @pytest.mark.asyncio
    async def test_stateless_structured_output_exhausted_books_cost(
        self, _backend: Any,
    ) -> None:
        from maestro.sdk import ErrorCategory
        err = await self._stateless(
            _billed_error_result(0.42), output_format=_SCHEMA,
        )
        assert err.category is ErrorCategory.STRUCTURED_OUTPUT_INVALID
        assert err.unbooked is None
        assert _backend.append.call_count == 1
        sample = _backend.append.call_args.args[0]
        assert sample.cost_usd == pytest.approx(0.42)
        assert sample.caller == "scout_opportunities"
        assert sample.input_tokens == 5000
        assert sample.output_tokens == 3000
        assert self.sink.call_count == 1

    @pytest.mark.asyncio
    async def test_stateless_unproductive_aggregate_books_cost(
        self, _backend: Any,
    ) -> None:
        from maestro.sdk import ErrorCategory
        err = await self._stateless(
            _billed_error_result(0.30, subtype="result", is_error=False),
        )
        assert err.category is ErrorCategory.STREAM_STALL
        assert self._booked(_backend) == pytest.approx([0.30])
        assert self.sink.call_count == 1

    @pytest.mark.asyncio
    async def test_session_structured_output_exhausted_books_delta_and_advances_baseline(
        self, _backend: Any,
    ) -> None:
        from maestro.sdk import ErrorCategory
        client = _session_client(
            [_cost_result(0.10)], [_billed_error_result(0.52)],
            [_cost_result(0.60)],
        )
        client._maestro_output_format = _SCHEMA
        await self._session_turn(client)
        err = await self._session_error(client)
        assert err.category is ErrorCategory.STRUCTURED_OUTPUT_INVALID
        assert client._maestro_cost_baseline == pytest.approx(0.52)
        await self._session_turn(client)
        assert self._booked(_backend) == pytest.approx([0.10, 0.42, 0.08])

    @pytest.mark.asyncio
    async def test_session_unproductive_aggregate_books_delta_and_advances_baseline(
        self, _backend: Any,
    ) -> None:
        from maestro.sdk import ErrorCategory
        client = _session_client(
            [_cost_result(0.10)],
            [_billed_error_result(0.25, subtype="result", is_error=False)],
            [_cost_result(0.30)],
        )
        await self._session_turn(client)
        err = await self._session_error(client)
        assert err.category is ErrorCategory.STREAM_STALL
        assert client._maestro_cost_baseline == pytest.approx(0.25)
        await self._session_turn(client)
        assert self._booked(_backend) == pytest.approx([0.10, 0.15, 0.05])

    @pytest.mark.asyncio
    async def test_session_rejected_rate_limit_books_cost(
        self, _backend: Any,
    ) -> None:
        import time

        from claude_agent_sdk.types import RateLimitEvent, RateLimitInfo

        from maestro.sdk import ErrorCategory
        rejected = RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="rejected", resets_at=int(time.time()) + 600,
            ),
            uuid="u1", session_id="s1",
        )
        client = _session_client(
            [rejected, _billed_error_result(0.20, subtype="result", is_error=False)],
        )
        err = await self._session_error(client)
        assert err.category is ErrorCategory.RATE_LIMIT
        assert self._booked(_backend) == pytest.approx([0.20])
        assert client._maestro_cost_baseline == pytest.approx(0.20)

    @pytest.mark.asyncio
    async def test_error_without_result_message_books_nothing(
        self, _backend: Any,
    ) -> None:
        from maestro.sdk import ErrorCategory
        client = _session_client([_cost_result(0.10)], [])
        await self._session_turn(client)
        err = await self._session_error(client)
        assert err.category is ErrorCategory.STREAM_STALL
        assert err.unbooked is None
        assert self._booked(_backend) == pytest.approx([0.10])
        assert client._maestro_cost_baseline == pytest.approx(0.10)

    @pytest.mark.asyncio
    async def test_session_reset_then_billed_error_rebases(
        self, _backend: Any,
    ) -> None:
        from claude_agent_sdk.types import ConversationResetMessage
        reset = ConversationResetMessage(
            new_conversation_id="c2", uuid="u", session_id="s1",
        )
        client = _session_client(
            [_cost_result(0.40)],
            [reset, _billed_error_result(0.07, subtype="result", is_error=False)],
        )
        await self._session_turn(client)
        await self._session_error(client)
        assert self._booked(_backend) == pytest.approx([0.40, 0.07])
        assert client._maestro_cost_baseline == pytest.approx(0.07)

    def test_booking_without_result_is_a_noop(self, _backend: Any) -> None:
        """An ``unbooked`` snapshot with no ResultMessage books nothing and
        is cleared."""
        from maestro.sdk import (
            QueryUsage,
            SDKError,
            _book_unbooked_error_result,
            _ConsumeResult,
        )
        err = SDKError("x")
        err.unbooked = _ConsumeResult(
            last=None, tool_calls=[], usage=QueryUsage(), rate_limits=[],
            rejected_resets_at=None,
        )
        assert _book_unbooked_error_result(err, cost_baseline=None) is None
        assert err.unbooked is None
        assert _backend.append.call_count == 0
