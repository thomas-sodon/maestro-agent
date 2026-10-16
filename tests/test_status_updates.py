"""Tests for loop status update notifications."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.agent import MaestroAgent, _format_status_message
from maestro.cost_tracking import CostRateSnapshot
from maestro.diagnostics import DiagnosticSnapshot
from maestro.notifications.base import NotifierTransportError
from maestro.sdk import IterationUsage
from tests.support.gap_fuel import stub_evolve_phase

_ZEROED_RATE = CostRateSnapshot(
    window_start="", window_end="", window_seconds=3600,
    total_cost_usd=0, total_input_tokens=0, total_output_tokens=0,
    total_cache_read_tokens=0, total_calls=0, cost_per_hour=0.0,
    cost_per_day=0, cost_per_month=0, tokens_in_per_hour=0,
    tokens_out_per_hour=0, by_caller={},
)
_OK_DIAG = DiagnosticSnapshot(
    recent_error_count=0, has_quota_warning=False,
    has_rate_limit=False, consecutive_failures=0,
    summary="All clear",
)


def _rate(cost_per_hour: float) -> CostRateSnapshot:
    return CostRateSnapshot(
        window_start="", window_end="", window_seconds=3600,
        total_cost_usd=cost_per_hour, total_input_tokens=0,
        total_output_tokens=0, total_cache_read_tokens=0, total_calls=1,
        cost_per_hour=cost_per_hour, cost_per_day=0, cost_per_month=0,
        tokens_in_per_hour=0, tokens_out_per_hour=0, by_caller={},
    )


# ---------------------------------------------------------------------------
# _format_status_message — pure function tests
# ---------------------------------------------------------------------------


class TestFormatStatusMessageIterationStart:
    """Tests for _format_status_message (iteration-start only)."""

    def test_with_run_rate(self) -> None:
        """Iteration-start format includes the run rate."""
        msg = _format_status_message(
            iteration=5,
            cost_rate=_rate(1.23), health_summary="All clear",
        )
        assert "<b>Iter 5</b> — Starting" in msg
        assert "<b>$1.23/hr</b>" in msg
        assert "this iter" not in msg
        assert "<b>Health:</b> All clear" in msg

    def test_zero_cost(self) -> None:
        """Zero run rate is displayed as $0.00/hr."""
        msg = _format_status_message(
            iteration=1,
            cost_rate=_rate(0.0), health_summary="All clear",
        )
        assert "<b>$0.00/hr</b>" in msg

    def test_no_cost_rate(self) -> None:
        """Graceful fallback when cost_rate is None."""
        msg = _format_status_message(
            iteration=3,
            cost_rate=None, health_summary="All clear",
        )
        assert "<b>Iter 3</b> — Starting" in msg
        assert "Cost: unknown" in msg
        assert "<b>" not in "Cost: unknown"
        assert "this iter" not in msg


class TestFormatStatusMessageHealth:
    """Tests for health summary in formatted messages."""

    def test_health_appears(self) -> None:
        """Health summary line is included in output."""
        msg = _format_status_message(
            iteration=1,
            cost_rate=None, health_summary="2 recent error(s)",
        )
        assert "<b>Health:</b> 2 recent error(s)" in msg

    def test_multi_fragment_health_renders_as_bullets(self) -> None:
        """Comma-separated health fragments split into a bullet list.

        The SDK summary is one logical group joined with ``" · "`` internally,
        so it survives as a single bullet while top-level commas split.
        """
        msg = _format_status_message(
            iteration=2,
            cost_rate=None,
            health_summary="2 recent error(s), sdk: 1 call · 3 in · 5 out · cache 100%",
        )
        assert "<b>Health:</b>\n" in msg
        assert "  • 2 recent error(s)" in msg
        assert "  • sdk: 1 call · 3 in · 5 out · cache 100%" in msg

    def test_health_summary_is_html_escaped(self) -> None:
        """Dynamic health summary is HTML-escaped."""
        msg = _format_status_message(
            iteration=1,
            cost_rate=None, health_summary="<hack>",
        )
        assert "&lt;hack&gt;" in msg
        assert "<hack>" not in msg

    def test_within_telegram_limit(self) -> None:
        """Output stays within Telegram's 4096 char limit."""
        msg = _format_status_message(
            iteration=999999,
            cost_rate=_rate(99999.99), health_summary="A" * 500,
        )
        assert len(msg) <= 4096


# ---------------------------------------------------------------------------
# _send_status_update — method tests
# ---------------------------------------------------------------------------


class TestSendStatusUpdate:
    """Tests for MaestroAgent._send_status_update."""

    @pytest.mark.asyncio
    async def test_calls_notifier(self, make_agent) -> None:  # type: ignore[no-untyped-def]
        """Calls notifier.notify with the formatted iteration-start status message."""
        agent: MaestroAgent = make_agent()
        agent.notifier = AsyncMock()  # type: ignore[assignment]
        agent._iteration_usage = IterationUsage(
            iteration=1, started_at="2026-01-01T00:00:00Z",
        )
        diag = DiagnosticSnapshot(
            recent_error_count=0, has_quota_warning=False,
            has_rate_limit=False, consecutive_failures=0,
            summary="All clear",
        )

        with patch("maestro.loop.lifecycle.build_cost_rates", return_value=_rate(0.50)):
            await agent._send_status_update(
                iteration=1, diag_snapshot=diag,
            )

        agent.notifier.notify.assert_called_once()
        msg = agent.notifier.notify.call_args.args[0]
        assert "<b>Iter 1</b> — Starting" in msg
        assert "<b>$0.50/hr</b>" in msg
        assert "<b>Health:</b> All clear" in msg
        assert agent.notifier.notify.call_args.kwargs["parse_mode"] == "HTML"

    @pytest.mark.asyncio
    async def test_best_effort_propagates_real_exception(self, make_agent) -> None:  # type: ignore[no-untyped-def]
        """A real ``Exception`` from the status notifier propagates.

        Status pings are cosmetic, but suppressing every ``Exception``
        also hid real bugs (broken Telegram client, network outage).
        Best-effort notification suppresses only ``CancelledError``;
        anything else is a bug and must surface for RCA.
        """
        agent: MaestroAgent = make_agent()
        agent.notifier = AsyncMock()  # type: ignore[assignment]
        agent.notifier.notify = AsyncMock(side_effect=RuntimeError("network"))

        with pytest.raises(RuntimeError, match="network"):
            await agent._send_status_update(
                iteration=1, diag_snapshot=None,
            )

    @pytest.mark.asyncio
    async def test_best_effort_swallows_cancelled_error(self, make_agent) -> None:  # type: ignore[no-untyped-def]
        # Reproduces the production fault chain where a residual SDK
        # teardown task (anyio cancel scope from _consume_messages
        # after early-exit) raised CancelledError into the next await
        # — a Telegram POST. Pre-fix this test failed because
        # `except Exception` does not match `BaseException`.
        agent: MaestroAgent = make_agent()
        agent.notifier = AsyncMock()  # type: ignore[assignment]
        agent.notifier.notify = AsyncMock(side_effect=asyncio.CancelledError())

        await agent._send_status_update(
            iteration=1, diag_snapshot=None,
        )

    @pytest.mark.asyncio
    async def test_best_effort_survives_notifier_transport_error(self, make_agent) -> None:  # type: ignore[no-untyped-def]
        """A transient notifier transport failure is absorbed, not fatal.

        The iteration-start status ping is a cosmetic telemetry
        side-channel; a brief Telegram transport timeout (surfaced as
        ``NotifierTransportError``) must not escape ``_notify_best_effort``
        and crash ``run_loop`` (issue #3476). Genuine bugs still propagate
        — see ``test_best_effort_propagates_real_exception``.
        """
        agent: MaestroAgent = make_agent()
        agent.notifier = AsyncMock()  # type: ignore[assignment]
        agent.notifier.notify = AsyncMock(
            side_effect=NotifierTransportError("timed out"),
        )

        await agent._send_status_update(
            iteration=1, diag_snapshot=None,
        )

    @pytest.mark.asyncio
    async def test_iteration_start_calls_build_cost_rates(self, make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
        """Iteration-start builds cost rates for run rate display."""
        agent: MaestroAgent = make_agent()
        agent.notifier = AsyncMock()  # type: ignore[assignment]

        with patch("maestro.loop.lifecycle.build_cost_rates", return_value=_rate(2.50)):
            await agent._send_status_update(
                iteration=1, diag_snapshot=None,
            )

        msg = agent.notifier.notify.call_args.args[0]
        assert "<b>$2.50/hr</b>" in msg

    @pytest.mark.asyncio
    async def test_uses_configured_window(self, make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
        """Window hours is read from config and passed through."""
        agent: MaestroAgent = make_agent()
        agent.config.budget_rate_window_hours = 2.5
        agent.notifier = AsyncMock()  # type: ignore[assignment]

        with patch("maestro.loop.lifecycle.build_cost_rates", return_value=_rate(0.10)) as mock_rates:
            await agent._send_status_update(
                iteration=1, diag_snapshot=None,
            )

        mock_rates.assert_called_once()
        assert mock_rates.call_args.kwargs["window_hours"] == 2.5
        msg = agent.notifier.notify.call_args.args[0]
        assert "<b>$0.10/hr</b>" in msg

    @pytest.mark.asyncio
    async def test_build_cost_rates_failure(self, make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
        """build_cost_rates failure is handled gracefully."""
        agent: MaestroAgent = make_agent()
        agent.notifier = AsyncMock()  # type: ignore[assignment]

        with patch("maestro.loop.lifecycle.build_cost_rates", side_effect=OSError("disk")):
            await agent._send_status_update(
                iteration=1, diag_snapshot=None,
            )

        agent.notifier.notify.assert_called_once()
        msg = agent.notifier.notify.call_args.args[0]
        assert "<b>Iter 1</b> — Starting" in msg
        assert "Cost: unknown" in msg

    @pytest.mark.asyncio
    async def test_status_carries_no_net_line(self, make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
        """The status reports cost alone: income is no longer folded in."""
        agent: MaestroAgent = make_agent()
        agent.notifier = AsyncMock()  # type: ignore[assignment]

        with patch("maestro.loop.lifecycle.build_cost_rates", return_value=_rate(2.0)):
            await agent._send_status_update(iteration=1, diag_snapshot=None)

        msg = agent.notifier.notify.call_args.args[0]
        assert "Net:" not in msg
        assert "<b>$2.00/hr</b>" in msg

    @pytest.mark.asyncio
    async def test_diag_none_shows_unknown_health(self, make_agent) -> None:  # type: ignore[no-untyped-def]
        """When diag_snapshot is None, health shows 'Unknown'."""
        agent: MaestroAgent = make_agent()
        agent.notifier = AsyncMock()  # type: ignore[assignment]

        await agent._send_status_update(
            iteration=1, diag_snapshot=None,
        )

        msg = agent.notifier.notify.call_args.args[0]
        assert "<b>Health:</b> Unknown" in msg


# ---------------------------------------------------------------------------
# _phase_will_run — gate helper unit tests
# ---------------------------------------------------------------------------


class TestPhaseWillRun:
    """Direct tests for the gate helper that drives phase-body execution."""

    def test_unknown_phase_defaults_to_run(self, make_agent) -> None:  # type: ignore[no-untyped-def]
        """Defensive default — unknown phase names return True so a newly
        added gated phase that lacks an entry here surfaces noisily rather
        than being silently skipped."""
        agent: MaestroAgent = make_agent()
        assert agent._phase_will_run(
            "nonexistent_phase",
            iteration=1, smoke=False, first_iteration_after_restart=True,
            task_every=0, evolve_every=999, dream_every=10,
            audit_self_every=0,
        ) is True

    @pytest.mark.parametrize(
        "phase,expected_when_throttled",
        [
            # Gated — return False under throttle.
            ("contemplate", False),
            ("implement_approved_plans", False),
            ("process_rejected_plans", False),
            ("generate_task", False),
            ("evolve", False),
            # Ungated — keep their base return value under throttle.
            # ``dream`` is zero-SDK since proposal 149 Step 5.
            ("dream", True),
            ("ensure_main_branch", True),
            ("check_contributions", True),
            ("process_messages", True),
            ("check_merged_plans", True),
            ("review_artifacts", True),
            ("tend_evolution_prs", True),
        ],
    )
    def test_budget_throttle_gates_only_heavy_llm_phases(  # type: ignore[no-untyped-def]
        self, make_agent, phase, expected_when_throttled,
    ) -> None:
        """During a burn-rate throttle, the heavy LLM phases (including
        ``contemplate``) are silently skipped while messaging, merge-checks,
        and tend-PRs keep running (zero-SDK-cost or intentionally responsive)."""
        agent: MaestroAgent = make_agent()
        agent._iter_message_context = ""
        agent._budget_throttle_entered_at = "2026-04-24T00:00:00+00:00"
        assert agent.budget_throttle_active is True
        assert (
            agent._phase_will_run(
                phase,
                iteration=10, smoke=False, first_iteration_after_restart=True,
                task_every=1, evolve_every=1, dream_every=1,
                audit_self_every=1,
            )
            is expected_when_throttled
        )

    def test_gate_is_throttle_scoped(self, make_agent) -> None:  # type: ignore[no-untyped-def]
        """Without the throttle flag set, gated phases return their base
        values — proving the gate is genuinely throttle-scoped and not
        blanket-hardcoded off."""
        agent: MaestroAgent = make_agent()
        agent._iter_message_context = ""
        agent._budget_throttle_entered_at = None
        assert agent.budget_throttle_active is False
        for phase in (
            "contemplate",
            "implement_approved_plans",
            "process_rejected_plans",
            "generate_task",
            "evolve",
            "dream",
        ):
            assert (
                agent._phase_will_run(
                    phase,
                    iteration=10, smoke=False,
                    first_iteration_after_restart=True,
                    task_every=1, evolve_every=1, dream_every=1,
                    audit_self_every=1,
                )
                is True
            ), f"{phase} should run when throttle inactive"

    def test_respond_to_messages_runs_under_throttle(  # type: ignore[no-untyped-def]
        self, make_agent,
    ) -> None:
        """``respond_to_messages`` is intentionally not in the gated set —
        Maestro stays responsive to Thomas during throttle."""
        agent: MaestroAgent = make_agent()
        agent._iter_message_context = "msg"
        agent._iter_reply_context = "msg"
        agent._budget_throttle_entered_at = "2026-04-24T00:00:00+00:00"
        assert agent.budget_throttle_active is True
        assert agent._phase_will_run(
            "respond_to_messages",
            iteration=10, smoke=False, first_iteration_after_restart=True,
            task_every=1, evolve_every=1, dream_every=1,
            audit_self_every=1,
        ) is True

    def test_smoke_mode_blocks_non_gated_phase_even_if_throttle_flag_set(  # type: ignore[no-untyped-def]
        self, make_agent,
    ) -> None:
        """Defence-in-depth: ``_compute_budget_throttle_multiplier`` never
        sets the throttle flag in smoke, but if the flag were somehow set
        the smoke gate still wins. Targets ``check_merged_plans`` — a phase
        *not* in ``_BUDGET_GATED_PHASES`` — so the throttle early-return at
        the top of ``_phase_will_run`` cannot fire and the smoke gate is the
        only path that can return ``False`` under this setup. The contrast
        assertion (``smoke=False``) proves the phase would otherwise run."""
        agent: MaestroAgent = make_agent()
        agent._budget_throttle_entered_at = "2026-04-24T00:00:00+00:00"
        assert agent._phase_will_run(
            "check_merged_plans",
            iteration=1, smoke=True, first_iteration_after_restart=True,
            task_every=1, evolve_every=1, dream_every=1,
            audit_self_every=1,
        ) is False
        assert agent._phase_will_run(
            "check_merged_plans",
            iteration=1, smoke=False, first_iteration_after_restart=True,
            task_every=1, evolve_every=1, dream_every=1,
            audit_self_every=1,
        ) is True


# ---------------------------------------------------------------------------
# run_loop integration — status updates wired correctly
# ---------------------------------------------------------------------------


def _prep_loop_agent(agent: MaestroAgent) -> MaestroAgent:
    """Stub out heavy loop phases so run_loop completes quickly."""
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    stub_evolve_phase(agent, AsyncMock())
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    agent.notifier = AsyncMock()  # type: ignore[assignment]
    return agent


class TestRunLoopStatusUpdates:
    """End-to-end tests for status updates in run_loop."""

    @pytest.mark.asyncio
    async def test_sends_iteration_start_status(self, make_agent) -> None:  # type: ignore[no-untyped-def]
        """run_loop sends exactly one iteration-start status notification."""
        agent = _prep_loop_agent(make_agent())

        with patch("maestro.loop.lifecycle.build_cost_rates", return_value=_ZEROED_RATE), \
             patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_OK_DIAG):
            await agent.run_loop(
                interval=0, max_iters=1, task_every=0, evolve_every=999,
            )

        messages = [c.args[0] for c in agent.notifier.notify.call_args_list]
        iter_starts = [m for m in messages if "<b>Iter 1</b> — Starting" in m]
        assert len(iter_starts) == 1

    @pytest.mark.asyncio
    async def test_runs_phase_body_when_gate_passes(self, make_agent) -> None:  # type: ignore[no-untyped-def]
        """When the gate fires, the phase body runs."""
        agent = _prep_loop_agent(make_agent())

        with patch("maestro.loop.lifecycle.build_cost_rates", return_value=_ZEROED_RATE), \
             patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_OK_DIAG):
            await agent.run_loop(
                interval=0, max_iters=1, task_every=0, evolve_every=1,
            )

        agent._evolve_with_fuel.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_notification_failure_crashes_loop(self, make_agent) -> None:  # type: ignore[no-untyped-def]
        """A real ``Exception`` from the iteration-start status crashes the loop.

        Status pings go through ``_notify_best_effort``, which
        suppresses only ``CancelledError``. A failing Telegram POST
        with a real ``Exception`` is a bug — broken client config,
        network outage, etc. — that should surface rather than be
        hidden under a per-iteration WARNING.
        """
        agent = _prep_loop_agent(make_agent())

        async def _flaky_notify(msg: str, parse_mode: str | None = None) -> None:
            if "Starting" in msg:
                raise RuntimeError("network")

        agent.notifier.notify = AsyncMock(side_effect=_flaky_notify)  # type: ignore[assignment]

        with (
            patch("maestro.loop.lifecycle.build_cost_rates", return_value=_ZEROED_RATE),
            patch(
                "maestro.diagnostics.build_diagnostic_snapshot",
                return_value=_OK_DIAG,
            ),
            pytest.raises(RuntimeError, match="network"),
        ):
            await agent.run_loop(
                interval=0, max_iters=1, task_every=0, evolve_every=999,
            )

        agent.contemplate.assert_not_called()

    @pytest.mark.asyncio
    async def test_diagnostic_snapshot_computed_once(self, make_agent) -> None:  # type: ignore[no-untyped-def]
        """Diagnostic snapshot is built once per iteration."""
        agent = _prep_loop_agent(make_agent())

        with patch("maestro.loop.lifecycle.build_cost_rates", return_value=_ZEROED_RATE), \
             patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_OK_DIAG) as mock_diag:
            await agent.run_loop(
                interval=0, max_iters=1, task_every=0, evolve_every=999,
            )

        assert mock_diag.call_count == 1

    @pytest.mark.asyncio
    async def test_hoisted_snapshot_passed_to_contemplate(self, make_agent) -> None:  # type: ignore[no-untyped-def]
        """run_loop threads the once-built snapshot into contemplate (#1211).

        ``test_diagnostic_snapshot_computed_once`` mocks ``contemplate``, so it
        only proves the *status* build fires once — it cannot see whether the
        loop reuses that snapshot. This asserts the end-to-end wiring: the
        snapshot the status block built (``_OK_DIAG``) is stored on
        ``self._iter_diag_snapshot`` and handed to the ``contemplate`` phase
        call. Without it, a regression re-adding the fresh-iteration reset the
        plan asked for — which runs *after* the status block and would clobber
        the field to ``None`` — would silently reintroduce the double build
        while every other test stayed green.
        """
        agent = _prep_loop_agent(make_agent())

        with patch("maestro.loop.lifecycle.build_cost_rates", return_value=_ZEROED_RATE), \
             patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_OK_DIAG):
            await agent.run_loop(
                interval=0, max_iters=1, task_every=0, evolve_every=999,
            )

        agent.contemplate.assert_awaited_once()
        assert agent.contemplate.call_args.kwargs["diag_snapshot"] is _OK_DIAG
        assert agent._iter_diag_snapshot is _OK_DIAG

    @pytest.mark.asyncio
    async def test_diagnostic_failure_uses_fallback_health(self, make_agent) -> None:  # type: ignore[no-untyped-def]
        """Diagnostic snapshot failure results in 'Unknown' health in the
        single iteration-start status message."""
        agent = _prep_loop_agent(make_agent())

        with patch("maestro.loop.lifecycle.build_cost_rates", return_value=_ZEROED_RATE), \
             patch(
                 "maestro.diagnostics.build_diagnostic_snapshot",
                 side_effect=OSError("log missing"),
             ):
            await agent.run_loop(
                interval=0, max_iters=1, task_every=0, evolve_every=999,
            )

        messages = [c.args[0] for c in agent.notifier.notify.call_args_list]
        status_msgs = [m for m in messages if "Health:" in m]
        assert len(status_msgs) == 1
        assert "<b>Health:</b> Unknown" in status_msgs[0]

    @pytest.mark.asyncio
    async def test_skips_status_update_when_iteration_frozen(self, make_agent) -> None:  # type: ignore[no-untyped-def]
        """Under budget throttle the iteration counter freezes, so two
        consecutive loop bodies share the same iteration value. The
        status update fires on the first body and dedups on the second.
        """
        agent = _prep_loop_agent(make_agent())
        # ``run_loop`` calls ``_read_lifecycle_progress`` at startup and
        # overwrites ``_budget_throttle_entered_at`` from the persisted
        # tuple — patch that read to inject an active throttle so the
        # iteration counter freezes from the first body.
        agent._read_lifecycle_progress = MagicMock(  # type: ignore[method-assign]
            return_value=(
                None, 0, None, None, False, "", "", None, None, None,
                "2026-04-24T00:00:00+00:00", False, None, 0, 0, 0, 0, False,
            ),
        )
        # Hold the throttle on across iterations — the default
        # transition check at end-of-iteration would otherwise observe
        # the patched zero-cost rate, compute multiplier 1.0, and
        # release the throttle before the second body starts.
        agent._maybe_notify_throttle_transition = AsyncMock()  # type: ignore[method-assign]

        with patch("maestro.loop.lifecycle.build_cost_rates", return_value=_ZEROED_RATE), \
             patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_OK_DIAG):
            await agent.run_loop(
                interval=0, max_iters=2, task_every=0, evolve_every=999,
            )

        messages = [c.args[0] for c in agent.notifier.notify.call_args_list]
        starting_msgs = [m for m in messages if "— Starting" in m]
        assert len(starting_msgs) == 1

    @pytest.mark.asyncio
    async def test_diagnostic_snapshot_skipped_when_iteration_frozen(self, make_agent) -> None:  # type: ignore[no-untyped-def]
        """Under freeze, the diagnostic snapshot build is gated by the
        same dedup check — proving the gating sits at the call site,
        not inside ``_send_status_update``."""
        agent = _prep_loop_agent(make_agent())
        agent._read_lifecycle_progress = MagicMock(  # type: ignore[method-assign]
            return_value=(
                None, 0, None, None, False, "", "", None, None, None,
                "2026-04-24T00:00:00+00:00", False, None, 0, 0, 0, 0, False,
            ),
        )
        agent._maybe_notify_throttle_transition = AsyncMock()  # type: ignore[method-assign]

        with patch("maestro.loop.lifecycle.build_cost_rates", return_value=_ZEROED_RATE), \
             patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_OK_DIAG) as mock_diag:
            await agent.run_loop(
                interval=0, max_iters=2, task_every=0, evolve_every=999,
            )

        assert mock_diag.call_count == 1

    @pytest.mark.asyncio
    async def test_sdk_iteration_marker_precedes_iteration_calls(  # type: ignore[no-untyped-def]
        self, make_agent, caplog,
    ) -> None:
        """The ``SDK iteration: iter=N`` marker is logged *before* the
        iteration's phases run, not after them (regression for #431).

        The marker delimits SDK-call log lines for a between-markers
        consumer, so it must precede the calls it groups. A phase that
        runs during the iteration logs a sentinel ``SDK call:`` line; the
        ``iter=1`` marker must appear before it. Pre-fix the marker was
        emitted at end-of-iteration, after the phases, so this ordering
        assertion failed.
        """
        agent = _prep_loop_agent(make_agent())

        async def _contemplate_logs_call(*args: object, **kwargs: object) -> str:
            logging.getLogger("maestro").info(
                "SDK call: caller=contemplate tokens_in=1 tokens_out=1",
            )
            return "reflection"

        agent.contemplate = AsyncMock(  # type: ignore[method-assign]
            side_effect=_contemplate_logs_call,
        )

        with caplog.at_level(logging.INFO, logger="maestro"), \
             patch("maestro.loop.lifecycle.build_cost_rates", return_value=_ZEROED_RATE), \
             patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_OK_DIAG):
            await agent.run_loop(
                interval=0, max_iters=1, task_every=0, evolve_every=999,
            )

        messages = [r.getMessage() for r in caplog.records]
        marker_idx = next(
            i for i, m in enumerate(messages) if m == "SDK iteration: iter=1"
        )
        call_idx = next(
            i for i, m in enumerate(messages)
            if m.startswith("SDK call: caller=contemplate")
        )
        assert marker_idx < call_idx
