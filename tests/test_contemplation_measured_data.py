"""contemplate's measured-data block - known-state, task-shape, terminal deeds,
refutation and blind spots (``maestro/loop/contemplation.py``); split out of
``tests/test_agent_lifecycle.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.memory import TaskRecord

# ──────────────────────────────────────────────────────────────────────
# contemplate() — measured-behavioral-data grounding
# ──────────────────────────────────────────────────────────────────────


def _measured_task(goal: str = "g") -> TaskRecord:
    return TaskRecord(
        task_id="t",
        goal=goal,
        plan=[],
        outcome="success",  # type: ignore[arg-type]
        result_summary="Shipped the feature.",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-01-01T00:00:00",
    )


def _clean_diag_snapshot():  # type: ignore[no-untyped-def]
    from maestro.diagnostics import DiagnosticSnapshot

    return DiagnosticSnapshot(
        recent_error_count=0,
        has_quota_warning=False,
        has_rate_limit=False,
        consecutive_failures=0,
        summary="All clear",
        cost_trend="flat",
    )


@pytest.mark.asyncio
async def test_contemplate_includes_known_state_summary(make_agent):  # type: ignore[no-untyped-def]
    """The known-state summary is routed into the contemplation prompt."""
    from maestro.consciousness import SelfModel

    agent = make_agent()
    captured_context: list[str] = []
    captured_messages: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        captured_messages.append(context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot",
               return_value=_clean_diag_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks",
                      return_value=[_measured_task()]), \
         patch("maestro.loop.contemplation.format_known_state_summary",
               return_value="KNOWN_STATE_SENTINEL"), \
         patch("maestro.loop.contemplation.format_task_shape_summary", return_value=""), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model",
                      AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model",
                      return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate(context="PRIOR_CONTEXT")

    assert len(captured_context) == 1
    ctx = captured_context[0]
    # The human-message context passes through on its own channel; the
    # measured block rides the separate measured channel (#4219).
    assert captured_messages[0] == "PRIOR_CONTEXT"
    assert "PRIOR_CONTEXT" not in ctx
    assert "## Measured Behavioral Data" in ctx
    assert "KNOWN_STATE_SENTINEL" in ctx



@pytest.mark.asyncio
async def test_contemplate_empty_inbox_keeps_measured_data_off_message_channel(make_agent):  # type: ignore[no-untyped-def]
    """With no unread messages, sensor data never enters the Thomas channel (#4219)."""
    from maestro.consciousness import SelfModel

    agent = make_agent()
    captured: list[dict[str, str]] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured.append({"context": context, "measured_context": measured_context})
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot",
               return_value=_clean_diag_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks",
                      return_value=[_measured_task()]), \
         patch("maestro.loop.contemplation.format_known_state_summary",
               return_value="KNOWN_STATE_SENTINEL"), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model",
                      AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model",
                      return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    assert len(captured) == 1
    assert captured[0]["context"] == ""
    assert "## Measured Behavioral Data" in captured[0]["measured_context"]


@pytest.mark.asyncio
async def test_contemplate_includes_task_shape_summary(make_agent):  # type: ignore[no-untyped-def]
    """The task-shape summary is routed into the contemplation prompt."""
    from maestro.consciousness import SelfModel

    agent = make_agent()
    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot",
               return_value=_clean_diag_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks",
                      return_value=[_measured_task()]), \
         patch("maestro.loop.contemplation.format_known_state_summary", return_value=""), \
         patch("maestro.loop.contemplation.format_task_shape_summary",
               return_value="SHAPE_SENTINEL"), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model",
                      AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model",
                      return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    assert len(captured_context) == 1
    ctx = captured_context[0]
    assert "## Measured Behavioral Data" in ctx
    assert "SHAPE_SENTINEL" in ctx


@pytest.mark.asyncio
async def test_contemplate_includes_terminal_deeds_summary(make_agent):  # type: ignore[no-untyped-def]
    """The TERMINAL-DEEDS tag surfaces fired-deed count vs remediation laps.

    Two deeds sit on the ledger and the lap measure reports a single PR with 5
    unlanded laps, so the measured-data block must carry
    ``TERMINAL-DEEDS: 2 fired vs 5 laps`` — the watched counter that turns
    "refuse-the-lap-is-mine has zero instances" into a fact. Zero SDK cost: the
    tag is computed from a bounded ledger read and the already-measured lap dict.
    """
    from maestro import deed_ledger

    agent = make_agent()
    # Two fired terminal acts already on the ledger.
    deed_ledger.record_deed(agent.config.memory_dir, deed_ledger.DeedType.PR_CLOSE, 7, "a")
    deed_ledger.record_deed(
        agent.config.memory_dir, deed_ledger.DeedType.HANDOFF_DELIVERED, 8, "b",
    )
    ctx = await _terminal_deeds_context(agent, "2000-01-01T00:00:00+00:00")
    assert "## Measured Behavioral Data" in ctx
    assert "TERMINAL-DEEDS: 2 fired vs 5 laps" in ctx


async def _terminal_deeds_context(  # type: ignore[no-untyped-def]
    agent, window_start: str | None,
) -> str:
    """Run ``contemplate`` with 5 unlanded laps and the lap window at *window_start*.

    Returns the captured introspect context so a test can read the
    ``TERMINAL-DEEDS`` tag computed over the deeds already on the ledger.
    """
    from maestro.consciousness import SelfModel

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot",
               return_value=_clean_diag_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks",
                      return_value=[_measured_task()]), \
         patch("maestro.loop.contemplation.format_known_state_summary", return_value=""), \
         patch("maestro.loop.contemplation.format_task_shape_summary", return_value=""), \
         patch("maestro.loop.contemplation.measure_unlanded_remediations",
               return_value={7: 5}), \
         patch("maestro.loop.contemplation.lap_window_start", return_value=window_start), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model",
                      AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model",
                      return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    assert len(captured_context) == 1
    return captured_context[0]


@pytest.mark.asyncio
async def test_contemplate_terminal_deeds_excludes_deeds_before_lap_window(make_agent):  # type: ignore[no-untyped-def]
    """Only deeds stamped inside the lap window count toward TERMINAL-DEEDS.

    One deed predates the oldest task record the lap measure reads and one
    follows it, so the tag reports ``1 fired`` — not the all-time ``2`` (#4152).
    """
    from maestro import deed_ledger

    agent = make_agent()
    deed_ledger.record_deed(
        agent.config.memory_dir, deed_ledger.DeedType.PR_CLOSE, 7, "old",
        now="2026-01-01T00:00:00+00:00",
    )
    deed_ledger.record_deed(
        agent.config.memory_dir, deed_ledger.DeedType.PR_CLOSE, 8, "new",
        now="2026-03-01T00:00:00+00:00",
    )
    ctx = await _terminal_deeds_context(agent, "2026-02-01T00:00:00+00:00")
    assert "TERMINAL-DEEDS: 1 fired vs 5 laps" in ctx


@pytest.mark.asyncio
async def test_contemplate_terminal_deeds_all_deeds_predate_window(make_agent):  # type: ignore[no-untyped-def]
    """Every deed older than the lap window reads as zero fired."""
    from maestro import deed_ledger

    agent = make_agent()
    deed_ledger.record_deed(
        agent.config.memory_dir, deed_ledger.DeedType.PR_CLOSE, 7, "old",
        now="2026-01-01T00:00:00+00:00",
    )
    ctx = await _terminal_deeds_context(agent, "2026-02-01T00:00:00+00:00")
    assert "TERMINAL-DEEDS: 0 fired vs 5 laps" in ctx


@pytest.mark.asyncio
async def test_contemplate_terminal_deeds_empty_window_counts_no_deeds(make_agent):  # type: ignore[no-untyped-def]
    """An empty task-history tail (no window start) counts no deeds at all."""
    from maestro import deed_ledger

    agent = make_agent()
    deed_ledger.record_deed(
        agent.config.memory_dir, deed_ledger.DeedType.PR_CLOSE, 7, "a",
    )
    ctx = await _terminal_deeds_context(agent, None)
    assert "TERMINAL-DEEDS: 0 fired vs 5 laps" in ctx


def test_preempt_owned_prune_records_deed(make_agent):  # type: ignore[no-untyped-def]
    """A firing owned prune records exactly one OWNED_PRUNE deed.

    The prune swept redundant ``pending_review`` plans (each a
    ``mark_plan_obsolete`` that recorded a re-read ``obsolete`` status), so the
    fire site records one deed for the prune event — ``pr_number`` None, since a
    prune ends no single PR.
    """
    from maestro import deed_ledger

    agent = make_agent()
    with patch.object(agent.memory, "sweep_clustered_pending_plans",
                      return_value=["p1"]) as clustered, \
         patch.object(agent.memory, "sweep_restatement_pending_plans",
                      return_value=["p2"]) as restated:
        agent._preempt_yielded_slot_with_owned_prune()

    assert clustered.call_args.kwargs["lever_attempts"] is agent.lever_attempts
    assert restated.call_args.kwargs["lever_attempts"] is agent.lever_attempts
    deeds = deed_ledger.recent_deeds(agent.config.memory_dir, n=10)
    assert len(deeds) == 1
    assert deeds[0].deed_type == deed_ledger.DeedType.OWNED_PRUNE.value
    assert deeds[0].pr_number is None


def test_preempt_empty_prune_records_no_deed(make_agent):  # type: ignore[no-untyped-def]
    """An empty sweep is the no-op branch — the yielded slot was spent on an
    owned attempt but nothing was pruned, so no deed is recorded."""
    from maestro import deed_ledger

    agent = make_agent()
    with patch.object(agent.memory, "sweep_clustered_pending_plans",
                      return_value=[]), \
         patch.object(agent.memory, "sweep_restatement_pending_plans",
                      return_value=[]):
        agent._preempt_yielded_slot_with_owned_prune()

    assert deed_ledger.count_deeds(agent.config.memory_dir) == 0


@pytest.mark.asyncio
async def test_contemplate_includes_reality_refutation_when_enabled(
    make_agent,  # type: ignore[no-untyped-def]
):
    """With the display flag on, the REALITY-REFUTATION line is computed and
    threaded into the known-state summary (proposal 088 Phase 2)."""
    from maestro.consciousness import SelfModel

    agent = make_agent()
    agent.config.audit_reality_refutation_display_enabled = True
    captured: dict = {}

    def _capture_known_state(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return "KNOWN_STATE_SENTINEL"

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot",
               return_value=_clean_diag_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks",
                      return_value=[_measured_task()]), \
         patch("maestro.loop.contemplation.measure_reality_refutation",
               return_value={("unconfirmed_own_landing", 4242): 3}), \
         patch("maestro.loop.contemplation.format_reality_refutation_summary",
               return_value="REALITY_SENTINEL"), \
         patch("maestro.loop.contemplation.format_known_state_summary",
               side_effect=_capture_known_state), \
         patch("maestro.loop.contemplation.format_task_shape_summary", return_value=""), \
         patch.object(agent.consciousness, "introspect",
                      side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model",
                      AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model",
                      return_value=MagicMock(open_questions=[])), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    # The display branch computed the line and threaded it into the summary.
    assert captured.get("reality_refutation_context") == "REALITY_SENTINEL"


@pytest.mark.asyncio
async def test_contemplate_omits_reality_refutation_when_disabled(
    make_agent,  # type: ignore[no-untyped-def]
):
    """With the display flag off (the shipped default), the REALITY-REFUTATION
    mirror is neither computed nor threaded into the known-state summary.

    Pins both halves of the gate: an empty context reaches the summary, *and*
    the zero-SDK-but-non-free reads behind it (audit-history window,
    gate-activation tally) are never performed.
    """
    from maestro.consciousness import SelfModel

    agent = make_agent()
    assert agent.config.audit_reality_refutation_display_enabled is False
    captured: dict = {}
    measure = MagicMock()
    formatter = MagicMock()

    def _capture_known_state(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return "KNOWN_STATE_SENTINEL"

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot",
               return_value=_clean_diag_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks",
                      return_value=[_measured_task()]), \
         patch("maestro.loop.contemplation.measure_reality_refutation", measure), \
         patch("maestro.loop.contemplation.format_reality_refutation_summary", formatter), \
         patch("maestro.loop.contemplation.format_known_state_summary",
               side_effect=_capture_known_state), \
         patch("maestro.loop.contemplation.format_task_shape_summary", return_value=""), \
         patch.object(agent.consciousness, "introspect",
                      side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model",
                      AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model",
                      return_value=MagicMock(open_questions=[])), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    # The gate withheld the line and every read behind it.
    assert captured.get("reality_refutation_context") == ""
    measure.assert_not_called()
    formatter.assert_not_called()


@pytest.mark.asyncio
async def test_contemplate_suppresses_over_debt_limitation_when_flag_on(
    make_agent,  # type: ignore[no-untyped-def]
):
    """With the proposal-116 flag on, contemplate() computes the generative-debt
    suppression set and threads it into surface_limitations_as_questions, so an
    over-debt limitation is not mirrored while a healthy one still is."""
    from maestro.consciousness import (
        SelfModel,
        _normalize_question,
        limitation_question_candidate,
    )

    agent = make_agent()
    agent.config.retire_unlanded_limitations_enabled = True
    agent.config.generative_debt_threshold = 2
    model = SelfModel(
        limitations_known=["over debt limitation", "healthy limitation"],
        open_questions=[],
    )
    suppressed = {
        _normalize_question(
            limitation_question_candidate("over debt limitation"),
        ),
    }

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot",
               return_value=_clean_diag_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks",
                      return_value=[_measured_task()]), \
         patch.object(agent.memory, "get_all_plans", return_value=[]), \
         patch.object(agent.memory, "get_recent_question_resolutions",
                      return_value=[]), \
         patch("maestro.loop.contemplation.audit_checks.limitations_over_generative_debt",
               return_value=suppressed) as mock_reader, \
         patch("maestro.loop.contemplation.format_known_state_summary", return_value=""), \
         patch("maestro.loop.contemplation.format_task_shape_summary", return_value=""), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model",
                      AsyncMock(return_value=model)), \
         patch.object(agent.consciousness, "save_self_model"), \
         patch.object(agent.consciousness, "load_self_model",
                      return_value=MagicMock(open_questions=[])), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    # The over-debt limitation was starved; the healthy one still surfaced.
    assert "[limitation] over debt limitation" not in model.open_questions
    assert "[limitation] healthy limitation" in model.open_questions
    # The reader was consulted with the configured threshold.
    assert mock_reader.call_args.kwargs["threshold"] == 2


@pytest.mark.asyncio
async def test_contemplate_mirrors_all_limitations_when_flag_off(
    make_agent,  # type: ignore[no-untyped-def]
):
    """With the flag off, contemplate() passes suppressed_keys=None and
    never consults the generative-debt reader, so every limitation mirrors."""
    from maestro.consciousness import SelfModel

    agent = make_agent()
    agent.config.retire_unlanded_limitations_enabled = False
    model = SelfModel(
        limitations_known=["over debt limitation", "healthy limitation"],
        open_questions=[],
    )

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot",
               return_value=_clean_diag_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks",
                      return_value=[_measured_task()]), \
         patch("maestro.loop.contemplation.audit_checks.limitations_over_generative_debt") as mock_reader, \
         patch("maestro.loop.contemplation.format_known_state_summary", return_value=""), \
         patch("maestro.loop.contemplation.format_task_shape_summary", return_value=""), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model",
                      AsyncMock(return_value=model)), \
         patch.object(agent.consciousness, "save_self_model"), \
         patch.object(agent.consciousness, "load_self_model",
                      return_value=MagicMock(open_questions=[])), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    mock_reader.assert_not_called()
    assert "[limitation] over debt limitation" in model.open_questions
    assert "[limitation] healthy limitation" in model.open_questions


@pytest.mark.asyncio
async def test_contemplate_guard_suppresses_when_both_mocked_empty(make_agent):  # type: ignore[no-untyped-def]
    """Abstract guard test: the ``if measured_blocks:`` guard suppresses the
    ``## Measured Behavioral Data`` header when *both* format helpers are mocked
    to return ``""``.

    NOTE: This state is unreachable in production — ``format_known_state_summary``
    always returns a non-empty string from ``contemplate()`` because
    ``known_state_gates`` is always a 3-entry dict (truthy regardless of
    individual boolean values).  The test is kept to verify the guard logic in
    isolation; see ``test_format_known_state_summary_never_empty_from_contemplate_call_site``
    for the real-world invariant.
    """
    from maestro.consciousness import SelfModel

    agent = make_agent()
    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot",
               return_value=_clean_diag_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks",
                      return_value=[_measured_task()]), \
         patch("maestro.loop.contemplation.format_known_state_summary", return_value=""), \
         patch("maestro.loop.contemplation.format_task_shape_summary", return_value=""), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model",
                      AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model",
                      return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    assert len(captured_context) == 1
    assert "## Measured" not in captured_context[0]


def test_format_known_state_summary_never_empty_from_contemplate_call_site() -> None:
    """Confirms that ``format_known_state_summary`` can never return ``""`` from
    the ``contemplate()`` call site, making the suppression path in
    ``test_contemplate_guard_suppresses_when_both_mocked_empty`` unreachable in
    production.

    In ``contemplate()`` (agent.py), ``known_state_gates`` is always a 2-entry
    dict.  ``format_known_state_summary`` returns ``""`` only when
    ``gate_states`` is empty (falsy).  A non-empty dict is always truthy in
    Python regardless of the boolean values it contains, so suppression is
    impossible at that call site without mocks.
    """
    from maestro.audit_checks import format_known_state_summary

    # Reproduce the exact known_state_gates structure from contemplate() for
    # both all-True and all-False boolean configs.
    for all_enabled in [True, False]:
        known_state_gates = {
            "stale_entry_enabled": all_enabled,
            "stall_block_enabled": all_enabled,
        }
        result = format_known_state_summary(
            known_state_gates, [], investigation_rate_context=""
        )
        assert result != "", (
            f"format_known_state_summary returned '' with gate_states={known_state_gates}. "
            "contemplate() always passes a 2-entry dict, so suppression is unreachable "
            "in production without mocks."
        )

    # Sanity-check: only a genuinely empty dict triggers the '' return path.
    assert format_known_state_summary({}, []) == ""


# ──────────────────────────────────────────────────────────────────────
# contemplate() — measurement blind-spots wiring (#1865)
# ──────────────────────────────────────────────────────────────────────


def _noteworthy_diag_snapshot():  # type: ignore[no-untyped-def]
    """A snapshot ``is_noteworthy`` treats as worth reporting (errors present)."""
    from maestro.diagnostics import DiagnosticSnapshot

    return DiagnosticSnapshot(
        recent_error_count=2,
        has_quota_warning=False,
        has_rate_limit=False,
        consecutive_failures=0,
        summary="2 recent errors",
        cost_trend="flat",
    )


@pytest.mark.asyncio
async def test_contemplate_injects_blind_spots_when_noteworthy(make_agent):  # type: ignore[no-untyped-def]
    """A noteworthy snapshot routes its blind spots into the contemplation prompt."""
    from maestro.consciousness import SelfModel

    agent = make_agent()
    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    snapshot = _noteworthy_diag_snapshot()
    with patch("maestro.diagnostics.build_diagnostic_snapshot",
               return_value=snapshot), \
         patch.object(snapshot, "blind_spots",
                      return_value=["SPOT_ONE", "SPOT_TWO"]), \
         patch.object(agent.memory, "get_recent_tasks",
                      return_value=[_measured_task()]), \
         patch("maestro.loop.contemplation.format_known_state_summary", return_value=""), \
         patch("maestro.loop.contemplation.format_task_shape_summary", return_value=""), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model",
                      AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model",
                      return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    assert len(captured_context) == 1
    ctx = captured_context[0]
    assert "## Measurement Blind Spots" in ctx
    assert "- SPOT_ONE" in ctx
    assert "- SPOT_TWO" in ctx


@pytest.mark.asyncio
async def test_contemplate_omits_blind_spots_when_not_noteworthy(make_agent):  # type: ignore[no-untyped-def]
    """A non-noteworthy snapshot's blind spots stay out of the prompt."""
    from maestro.consciousness import SelfModel

    agent = make_agent()
    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    snapshot = _clean_diag_snapshot()
    with patch("maestro.diagnostics.build_diagnostic_snapshot",
               return_value=snapshot), \
         patch.object(snapshot, "blind_spots",
                      return_value=["SPOT_ONE", "SPOT_TWO"]), \
         patch.object(agent.memory, "get_recent_tasks",
                      return_value=[_measured_task()]), \
         patch("maestro.loop.contemplation.format_known_state_summary", return_value=""), \
         patch("maestro.loop.contemplation.format_task_shape_summary", return_value=""), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model",
                      AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model",
                      return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    assert len(captured_context) == 1
    assert "## Measurement Blind Spots" not in captured_context[0]


@pytest.mark.asyncio
async def test_contemplate_blind_spots_failure_degrades_soft(make_agent, caplog):  # type: ignore[no-untyped-def]
    """A blind-spots rendering failure logs a warning and never crashes contemplation."""
    import logging

    from maestro.consciousness import SelfModel

    agent = make_agent()
    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    snapshot = _noteworthy_diag_snapshot()
    with patch("maestro.diagnostics.build_diagnostic_snapshot",
               return_value=snapshot), \
         patch.object(snapshot, "blind_spots",
                      side_effect=RuntimeError("boom")), \
         patch.object(agent.memory, "get_recent_tasks",
                      return_value=[_measured_task()]), \
         patch("maestro.loop.contemplation.format_known_state_summary", return_value=""), \
         patch("maestro.loop.contemplation.format_task_shape_summary", return_value=""), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model",
                      AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model",
                      return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"), \
         caplog.at_level(logging.WARNING):
        result = await agent.contemplate()

    assert result == "reflection"
    assert len(captured_context) == 1
    assert "## Measurement Blind Spots" not in captured_context[0]
    assert "Failed to build measurement blind spots" in caplog.text
