"""Component tests for the ``review_artifacts`` loop phase dispatch."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from tests.component.conftest import write_self_model
from tests.component.sdk_mock import SDKMock


@pytest.mark.asyncio
async def test_review_artifacts_runs_on_first_iteration(
    make_agent, memory_dir,
):
    """First iteration always runs the phase when ``artifact_review_enabled``."""
    write_self_model(memory_dir)
    agent, _ch, _vcs = make_agent()
    sdk = SDKMock()
    sdk.then(
        "Reflection", json.dumps({"identity_statements": ["I am"]}),
    )
    with sdk.patch("maestro.consciousness"), \
         patch.object(agent, "_install_signal_handlers"), \
         patch(
             "maestro.artifact_review.ArtifactReviewer.review",
             new=AsyncMock(return_value=[]),
         ) as review_mock, \
         patch(
             "maestro.artifact_review.ArtifactReviewer.select_candidates",
             return_value=[object()],  # any truthy value to trigger review()
         ):
        # ``review_every=999`` so only first-iteration branch can fire.
        agent.config.artifact_review_enabled = True
        agent.config.artifact_review_every = 999
        await agent.run_loop(interval=0, max_iters=1, evolve_every=10)
    review_mock.assert_awaited_once()
    kwargs = review_mock.await_args.kwargs
    assert kwargs["budget_throttle_active"] is False


@pytest.mark.asyncio
async def test_review_artifacts_disabled_never_runs(make_agent, memory_dir):
    """``artifact_review_enabled=False`` prevents the phase from running."""
    write_self_model(memory_dir)
    agent, _ch, _vcs = make_agent()
    agent.config.artifact_review_enabled = False
    sdk = SDKMock()
    sdk.then(
        "Reflection", json.dumps({"identity_statements": ["I am"]}),
    )
    with sdk.patch("maestro.consciousness"), \
         patch.object(agent, "_install_signal_handlers"), \
         patch(
             "maestro.artifact_review.ArtifactReviewer.review",
             new=AsyncMock(return_value=[]),
         ) as review_mock, \
         patch(
             "maestro.artifact_review.ArtifactReviewer.select_candidates",
             return_value=[object()],
         ) as select_mock:
        await agent.run_loop(interval=0, max_iters=1, evolve_every=10)
    review_mock.assert_not_called()
    select_mock.assert_not_called()


@pytest.mark.asyncio
async def test_review_artifacts_smoke_mode_bypasses(make_agent, memory_dir):
    """Smoke mode skips both artifact review and publication execution."""
    write_self_model(memory_dir)
    agent, _ch, _vcs = make_agent()
    agent.config.artifact_review_enabled = True
    agent.config.publication_enabled = True
    sdk = SDKMock()
    sdk.then(
        "Reflection", json.dumps({"identity_statements": ["I am"]}),
    )
    with sdk.patch("maestro.consciousness"), \
         patch.object(agent, "_install_signal_handlers"), \
         patch(
             "maestro.artifact_review.ArtifactReviewer.review",
             new=AsyncMock(return_value=[]),
         ) as review_mock, \
         patch(
             "maestro.artifact_review.ArtifactReviewer.select_candidates",
             return_value=[object()],
         ) as select_mock, \
         patch(
             "maestro.publication.Publisher.tick",
             new=AsyncMock(),
         ) as publish_tick_mock:
        await agent.run_loop(
            interval=0, max_iters=1, evolve_every=10, smoke=True,
        )
    review_mock.assert_not_called()
    select_mock.assert_not_called()
    publish_tick_mock.assert_not_called()


@pytest.mark.asyncio
async def test_review_artifacts_propagates_budget_throttle(
    make_agent, memory_dir,
):
    """Under burn-rate throttle, phase runs but review() receives
    ``budget_throttle_active=True`` so the LLM call inside is gated."""
    write_self_model(memory_dir)
    agent, _ch, _vcs = make_agent()
    agent.config.artifact_review_enabled = True
    # Persist throttle flag so run_loop's startup restore picks it up.
    (memory_dir / "lifecycle.json").write_text(json.dumps({
        "iteration": 0,
        "next_phase": None,
        "budget_throttle_entered_at": "2026-04-22T00:00:00+00:00",
    }))

    sdk = SDKMock()
    sdk.then(
        "Reflection", json.dumps({"identity_statements": ["I am"]}),
    )
    with sdk.patch("maestro.consciousness"), \
         patch.object(agent, "_install_signal_handlers"), \
         patch(
             "maestro.artifact_review.ArtifactReviewer.review",
             new=AsyncMock(return_value=[]),
         ) as review_mock, \
         patch(
             "maestro.artifact_review.ArtifactReviewer.select_candidates",
             return_value=[object()],
         ):
        await agent.run_loop(interval=0, max_iters=1, evolve_every=10)
    review_mock.assert_awaited_once()
    kwargs = review_mock.await_args.kwargs
    assert kwargs["budget_throttle_active"] is True


@pytest.mark.asyncio
async def test_review_artifacts_no_candidates_skips_llm_call(
    make_agent, memory_dir,
):
    """``select_candidates`` returning empty list short-circuits review()."""
    write_self_model(memory_dir)
    agent, _ch, _vcs = make_agent()
    agent.config.artifact_review_enabled = True
    sdk = SDKMock()
    sdk.then(
        "Reflection", json.dumps({"identity_statements": ["I am"]}),
    )
    with sdk.patch("maestro.consciousness"), \
         patch.object(agent, "_install_signal_handlers"), \
         patch(
             "maestro.artifact_review.ArtifactReviewer.review",
             new=AsyncMock(return_value=[]),
         ) as review_mock, \
         patch(
             "maestro.artifact_review.ArtifactReviewer.select_candidates",
             return_value=[],
         ):
        await agent.run_loop(interval=0, max_iters=1, evolve_every=10)
    review_mock.assert_not_called()


@pytest.mark.asyncio
async def test_execute_approved_publications_dispatches_when_enabled(
    make_agent, memory_dir,
):
    """The publication phase calls ``Publisher.tick`` every iteration
    when ``publication_enabled`` is on."""
    write_self_model(memory_dir)
    agent, _ch, _vcs = make_agent()
    agent.config.publication_enabled = True
    sdk = SDKMock()
    sdk.then(
        "Reflection", json.dumps({"identity_statements": ["I am"]}),
    )
    with sdk.patch("maestro.consciousness"), \
         patch.object(agent, "_install_signal_handlers"), \
         patch(
             "maestro.publication.Publisher.tick",
             new=AsyncMock(),
         ) as publish_tick_mock:
        await agent.run_loop(interval=0, max_iters=1, evolve_every=10)
    publish_tick_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_approved_publications_disabled_never_runs(
    make_agent, memory_dir,
):
    """``publication_enabled=False`` short-circuits the phase."""
    write_self_model(memory_dir)
    agent, _ch, _vcs = make_agent()
    agent.config.publication_enabled = False
    sdk = SDKMock()
    sdk.then(
        "Reflection", json.dumps({"identity_statements": ["I am"]}),
    )
    with sdk.patch("maestro.consciousness"), \
         patch.object(agent, "_install_signal_handlers"), \
         patch(
             "maestro.publication.Publisher.tick",
             new=AsyncMock(),
         ) as publish_tick_mock:
        await agent.run_loop(interval=0, max_iters=1, evolve_every=10)
    publish_tick_mock.assert_not_called()
