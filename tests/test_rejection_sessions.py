"""Tests for the rejection-phase session pair (``maestro/loop/rejection_sessions.py``).

The pair exists because the rejection classifier and the self-model update
carry different effort policies; handing one session to both silently demotes
the self-model call to the classifier's ``effort="low"`` (#3803).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.loop.rejection_sessions import rejection_sessions
from maestro.memory import EvolutionPlan


@pytest.mark.asyncio
async def test_rejection_sessions_opens_low_and_high_effort_pair() -> None:
    """Enabled: two sessions, low effort for the classifier and high for the
    self-model update, each with its own caller, both disconnected after."""
    classifier = MagicMock(name="classifier")
    self_model = MagicMock(name="self_model")

    with patch(
        "maestro.loop.rejection_sessions.create_session",
        new_callable=AsyncMock, side_effect=[classifier, self_model],
    ) as mock_create, patch(
        "maestro.loop.rejection_sessions.disconnect_session",
        new_callable=AsyncMock,
    ) as mock_disconnect:
        async with rejection_sessions(
            enabled=True, identity=lambda: "identity",
        ) as (got_classifier, got_self_model):
            assert got_classifier is classifier
            assert got_self_model is self_model
            assert mock_disconnect.await_count == 0

    assert mock_create.await_count == 2
    options = [c.kwargs["options"] for c in mock_create.call_args_list]
    assert options[0].effort == "low"
    assert options[0].caller == "rejection_session"
    assert options[1].effort == "high"
    assert options[1].caller == "rejection_self_model_session"
    assert options[1].tools == []
    assert mock_create.call_args_list[1].kwargs["system"] == "identity"
    assert mock_create.call_args_list[1].kwargs["phase"] == "rejection"
    disconnected = [c.args[0] for c in mock_disconnect.call_args_list]
    assert disconnected == [classifier, self_model]


@pytest.mark.asyncio
async def test_rejection_sessions_disabled_opens_nothing() -> None:
    """Disabled: both are ``None``, no session is opened, and the identity
    system prompt is not even built."""
    identity_calls: list[int] = []

    def _identity() -> str:
        identity_calls.append(1)
        return "identity"

    with patch(
        "maestro.loop.rejection_sessions.create_session",
        new_callable=AsyncMock,
    ) as mock_create, patch(
        "maestro.loop.rejection_sessions.disconnect_session",
        new_callable=AsyncMock,
    ) as mock_disconnect:
        async with rejection_sessions(
            enabled=False, identity=_identity,
        ) as (classifier, self_model):
            assert classifier is None
            assert self_model is None

    mock_create.assert_not_called()
    mock_disconnect.assert_not_called()
    assert identity_calls == []


@pytest.mark.asyncio
async def test_rejection_sessions_teardown_swallows_and_continues() -> None:
    """A failing disconnect is debug-swallowed and the second session is
    still torn down; the body's own exception still propagates."""
    classifier = MagicMock(name="classifier")
    self_model = MagicMock(name="self_model")
    disconnect = AsyncMock(side_effect=[RuntimeError("dc boom"), None])

    with patch(
        "maestro.loop.rejection_sessions.create_session",
        new_callable=AsyncMock, side_effect=[classifier, self_model],
    ), patch(
        "maestro.loop.rejection_sessions.disconnect_session", disconnect,
    ), pytest.raises(ValueError, match="body boom"):
        async with rejection_sessions(
            enabled=True, identity=lambda: "identity",
        ):
            raise ValueError("body boom")

    assert [c.args[0] for c in disconnect.call_args_list] == [
        classifier, self_model,
    ]


@pytest.mark.asyncio
async def test_process_rejected_plans_threads_self_model_session(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The separate high-effort self-model session is forwarded to every
    ``learn_from_rejection`` call alongside the classifier session (#3803)."""
    agent = make_agent()
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="rej_sm", title="r",
        category="capability", rationale="test",
        proposed_changes=[], files_affected=[], expected_impact="",
        status="rejected", rejection_reason="why",
        created_at="2025-01-01T00:00:00Z",
    ))
    agent.evolution.learn_from_rejection = AsyncMock()

    classifier_session = MagicMock(name="rejection_session")
    self_model_session = MagicMock(name="rejection_self_model_session")
    await agent._process_rejected_plans(
        session=classifier_session,
        self_model_session=self_model_session,
    )

    call = agent.evolution.learn_from_rejection.call_args
    assert call.kwargs["session"] is classifier_session
    assert call.kwargs["self_model_session"] is self_model_session


@pytest.mark.asyncio
async def test_phase_process_rejected_plans_opens_self_model_session(make_agent) -> None:  # type: ignore[no-untyped-def]
    """With a pending rejection and session reuse on, the phase opens the
    pair and hands the self-model session down (#3803)."""
    agent = make_agent()
    agent.config.enable_session_reuse = True
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="rej_phase", title="r",
        category="capability", rationale="test",
        proposed_changes=[], files_affected=[], expected_impact="",
        status="rejected", rejection_reason="why",
        created_at="2025-01-01T00:00:00Z",
    ))
    agent._process_rejected_plans = AsyncMock()

    classifier_session = MagicMock(name="rejection_session")
    self_model_session = MagicMock(name="rejection_self_model_session")
    with patch(
        "maestro.loop.rejection_sessions.create_session",
        new_callable=AsyncMock,
        side_effect=[classifier_session, self_model_session],
    ) as mock_create, patch(
        "maestro.loop.rejection_sessions.disconnect_session",
        new_callable=AsyncMock,
    ):
        await agent._phase_process_rejected_plans(iteration=1)

    efforts = [c.kwargs["options"].effort for c in mock_create.call_args_list]
    assert efforts == ["low", "high"]
    kwargs = agent._process_rejected_plans.call_args.kwargs
    assert kwargs["session"] is classifier_session
    assert kwargs["self_model_session"] is self_model_session
