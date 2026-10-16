"""Tests for Dashboard routes — fully mocked I/O."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient

from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.evolution import EvolutionEngine
from maestro.executor import Executor
from maestro.journal import Journal
from maestro.memory import EvolutionPlan, Memory
from maestro.notifications.base import NullNotifier
from maestro.planner import Planner

FAKE_DIR = Path("/fake/dash")


def _make_app(  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
    *,
    max_page_size: int = 500,
    log_buffer_size: int = 500,
    account_ui_v2: bool = True,
):
    from maestro.agent import MaestroAgent
    from maestro.dashboard.app import create_app

    config = Config(
        memory_dir=FAKE_DIR,
        workspace=FAKE_DIR,
        max_page_size=max_page_size,
        log_buffer_size=log_buffer_size,
        account_ui_v2=account_ui_v2,
    )
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


@pytest.mark.asyncio
async def test_health_endpoint(mem_fs: dict[str, str]) -> None:
    app, _ = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_account_returns_dashboard_html(mem_fs: dict[str, str]) -> None:
    app, _ = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/account")
    assert r.status_code == 200
    assert "Maestro — Account" in r.text


@pytest.mark.asyncio
async def test_account_flag_off_rollback_serves_template_byte_identical(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 122 rollback path: turning ``account_ui_v2`` off restores the
    flat pill nav.

    Since the 2026-09-03 graduation the flag ships on, so the off-posture is
    reached only by an explicit config edit — the one-line rollback the burn-in
    window depends on. Passing ``account_ui_v2=False`` here exercises exactly
    that: no marker is stamped and the response is byte-identical to the
    template, so the v2 shell leaves no residue on the rolled-back surface. The
    shipped default itself is asserted by
    ``tests/test_config.py::test_config_account_ui_v2_default_on``.
    """
    from maestro.dashboard.routes import TEMPLATES_DIR

    template = (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
    app, _ = _make_app(mem_fs, account_ui_v2=False)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/account")
    assert r.status_code == 200
    assert "data-account-ui-v2" not in r.text
    assert r.text == template


@pytest.mark.asyncio
async def test_account_flag_on_injects_body_marker(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 122: with ``account_ui_v2`` on — the shipped default since the
    2026-09-03 graduation, so ``_make_app`` needs no override — the handler
    stamps exactly one ``data-account-ui-v2`` marker onto the single <body>
    tag."""
    app, _ = _make_app(mem_fs)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/account")
    assert r.status_code == 200
    assert '<body data-account-ui-v2="true">' in r.text
    assert r.text.count("data-account-ui-v2") == 1


@pytest.mark.asyncio
async def test_root_returns_public_page(mem_fs: dict[str, str]) -> None:
    app, _ = _make_app(mem_fs)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/")
        # The identity fetch URL now lives in the externalized JS file,
        # so pull it back over the same server we just hit.
        js = await client.get("/static/public.js")
    assert r.status_code == 200
    # Public page title is just "Maestro" — no hyphen suffix.
    assert "<title>Maestro</title>" in r.text
    assert "/account" in r.text
    # The fetch URL moved to the extracted JS bundle.
    assert js.status_code == 200
    assert "/api/public/identity" in js.text


@pytest.mark.asyncio
async def test_html_shell_responses_disable_caching(mem_fs: dict[str, str]) -> None:
    """HTML shells must set ``Cache-Control: no-cache`` so a cached shell
    can't pin browsers to stale static-asset paths after an extraction."""
    app, _ = _make_app(mem_fs)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        public = await client.get("/")
        account = await client.get("/account")
    assert public.headers.get("cache-control") == "no-cache"
    assert account.headers.get("cache-control") == "no-cache"


@pytest.mark.asyncio
async def test_status_endpoint(mem_fs: dict[str, str]) -> None:
    app, _ = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/status")
    assert r.status_code == 200
    data = r.json()
    assert "stage" not in data
    assert data["running"] is False


@pytest.mark.asyncio
async def test_status_reports_page_size_limits(mem_fs: dict[str, str]) -> None:
    """``/api/status`` reports the page-size ceilings the client clamps to (#4593)."""
    app, _ = _make_app(mem_fs, log_buffer_size=100, max_page_size=25)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/status")
    assert r.status_code == 200
    assert r.json()["limits"] == {"max_page_size": 25, "log_buffer_size": 100}


@pytest.mark.asyncio
async def test_client_page_sizes_clamped_under_lowered_caps(mem_fs: dict[str, str]) -> None:
    """Emulate the client's ``clampPageSize``: under lowered caps the clamped
    ``n`` succeeds where the old hardcoded literals 422 (#4593)."""
    app, _ = _make_app(mem_fs, log_buffer_size=100, max_page_size=25)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        limits = (await client.get("/api/status")).json()["limits"]
        logs_n = min(200, limits["log_buffer_size"])
        reviews_n = min(50, limits["max_page_size"])
        logs = await client.get(f"/api/logs?n={logs_n}")
        reviews = await client.get(f"/api/artifact-reviews?n={reviews_n}")
        old_logs = await client.get("/api/logs?n=200")
        old_reviews = await client.get("/api/artifact-reviews?n=50")
    assert logs.status_code == 200
    assert reviews.status_code == 200
    assert old_logs.status_code == 422
    assert old_reviews.status_code == 422


@pytest.mark.asyncio
async def test_bare_page_sized_fetch_ok_below_default(mem_fs: dict[str, str]) -> None:
    """The bootstrap path omits ``n``; a ceiling below the route default must
    not 422 it — route defaults are clamped to the ceiling."""
    from maestro.memory import ArtifactReview

    app, agent = _make_app(mem_fs, max_page_size=5)
    for i in range(8):
        agent.memory.write_artifact_review(ArtifactReview(
            review_id=f"r{i}", task_id=f"t{i}", verdict="publish",
            title=f"t{i}", reasoning="r",
            bundle_file_names=["README.md"],
            proposed_name=f"n{i}", proposed_description="d",
            timestamp=f"2026-04-0{i + 1}T00:00:00+00:00",
        ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/artifact-reviews")
    assert r.status_code == 200
    assert len(r.json()) == 5


@pytest.mark.asyncio
async def test_consciousness_endpoint(mem_fs: dict[str, str]) -> None:
    app, _ = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/consciousness")
    assert r.status_code == 200
    data = r.json()
    assert "stage" not in data
    assert "open_questions" in data


@pytest.mark.asyncio
async def test_diagnostics_endpoint_no_log(mem_fs: dict[str, str]) -> None:
    app, _ = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/diagnostics")
    assert r.status_code == 200
    data = r.json()
    assert "recent_error_count" in data
    assert "summary" in data
    assert data["sdk_usage"] is None
    assert data["burn_rate_usd_per_hour"] == 0.0
    assert data["max_loop_budget_usd_per_hour"] == 0.0
    assert data["budget_throttle_active"] is False
    assert data["budget_throttle_entered_at"] is None


@pytest.mark.asyncio
async def test_diagnostics_endpoint_with_sdk_usage(mem_fs: dict[str, str]) -> None:

    from maestro.diagnostics import DiagnosticSnapshot, SDKUsageStats

    app, _ = _make_app(mem_fs)
    snapshot = DiagnosticSnapshot(
        recent_error_count=1,
        has_quota_warning=False,
        has_rate_limit=False,
        consecutive_failures=0,
        summary="ok",
        sdk_usage=SDKUsageStats(total_calls=3),
    )
    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=snapshot):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.get("/api/diagnostics")
    assert r.status_code == 200
    data = r.json()
    assert data["sdk_usage"] is not None
    assert data["sdk_usage"]["total_calls"] == 3


@pytest.mark.asyncio
async def test_journal_endpoint(mem_fs: dict[str, str]) -> None:
    app, agent = _make_app(mem_fs)
    agent.journal.write_thought("Test thought", "reflection", triggered_by="agent_tool")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/journal")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["content"] == "Test thought"


@pytest.mark.asyncio
async def test_journal_events_source(mem_fs: dict[str, str]) -> None:
    """``source=events`` serves operational records; the default never does."""
    app, agent = _make_app(mem_fs)
    agent.journal.write_thought("Inner thought", triggered_by="contemplation")
    agent.journal.record_event("Pre-deploy notice", entry_type="milestone", triggered_by="pre_deploy")
    agent.journal.record_event("Audit finding", entry_type="audit_observation", triggered_by="audit_self_model")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        thoughts = await client.get("/api/journal")
        events = await client.get("/api/journal?source=events")
        milestones = await client.get("/api/journal?source=events&entry_type=milestone")
        explicit = await client.get("/api/journal?source=thoughts")
    assert [e["content"] for e in thoughts.json()] == ["Inner thought"]
    assert [e["content"] for e in explicit.json()] == ["Inner thought"]
    assert [e["content"] for e in events.json()] == ["Pre-deploy notice", "Audit finding"]
    assert [e["content"] for e in milestones.json()] == ["Pre-deploy notice"]


@pytest.mark.asyncio
async def test_journal_invalid_source_rejected(mem_fs: dict[str, str]) -> None:
    app, _ = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/journal?source=logs")
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_journal_pagination_capped(mem_fs: dict[str, str]) -> None:
    app, _ = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/journal?n=99999")
    # FastAPI Query validation should cap or reject
    assert r.status_code == 422  # Validation error for n > MAX_PAGE_SIZE


@pytest.mark.asyncio
async def test_metrics_endpoint(mem_fs: dict[str, str]) -> None:
    app, _ = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/metrics")
    assert r.status_code == 200
    data = r.json()
    assert "success_rate" in data
    assert "stage" not in data


@pytest.mark.asyncio
async def test_metrics_total_tasks_correct_count(mem_fs: dict[str, str]) -> None:
    """total_tasks in metrics must reflect the exact number of recorded tasks."""
    from maestro.memory import TaskRecord
    app, agent = _make_app(mem_fs)
    for i in range(3):
        agent.memory.record_task(TaskRecord(
            task_id=f"t{i}", goal=f"Goal {i}", plan=[],
            outcome="success", result_summary="done",
            errors=[], duration_secs=1.0,
            timestamp="2025-01-01T00:00:00Z",
        ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/metrics")
    assert r.status_code == 200
    assert r.json()["total_tasks"] == 3


@pytest.mark.asyncio
async def test_plans_endpoint(mem_fs: dict[str, str]) -> None:
    app, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="p1", title="Test Plan",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/plans")
    assert r.status_code == 200
    assert len(r.json()) == 1


@pytest.mark.asyncio
async def test_plans_endpoint_status_filter(mem_fs: dict[str, str]) -> None:
    app, agent = _make_app(mem_fs)
    for pid, status in [("ppend", "pending_review"), ("papp", "approved"), ("prej", "rejected")]:
        agent.memory.save_evolution_plan(EvolutionPlan(
            plan_id=pid, title=f"Plan {pid}",
            category="capability", rationale="test", proposed_changes=[],
            files_affected=[], expected_impact="", status=status,
            created_at="2025-01-01T00:00:00Z",
        ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r_all = await client.get("/api/plans")
        r_pending = await client.get("/api/plans?status=pending_review")
        r_rejected = await client.get("/api/plans?status=rejected")
        r_invalid = await client.get("/api/plans?status=bogus")
    assert len(r_all.json()) == 3
    assert len(r_pending.json()) == 1
    assert r_pending.json()[0]["plan_id"] == "ppend"
    assert len(r_rejected.json()) == 1
    assert r_invalid.status_code == 400


@pytest.mark.asyncio
async def test_plans_endpoint_accepts_blocked_status_filter(
    mem_fs: dict[str, str],
) -> None:
    """131: ``?status=blocked`` is an accepted (non-400) filter value — the new
    status is auto-valid because VALID_PLAN_STATUSES derives from
    get_args(PlanStatus) — and filters to exactly the blocked plan."""
    app, agent = _make_app(mem_fs)
    for pid, status in [("papp", "approved"), ("pblk", "blocked")]:
        agent.memory.save_evolution_plan(EvolutionPlan(
            plan_id=pid, title=f"Plan {pid}",
            category="capability", rationale="test", proposed_changes=[],
            files_affected=[], expected_impact="", status=status,  # type: ignore[arg-type]
            created_at="2025-01-01T00:00:00Z",
        ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r_blocked = await client.get("/api/plans?status=blocked")
    assert r_blocked.status_code == 200
    assert len(r_blocked.json()) == 1
    assert r_blocked.json()[0]["plan_id"] == "pblk"


@pytest.mark.asyncio
async def test_plans_endpoint_n_pagination(mem_fs: dict[str, str]) -> None:
    app, agent = _make_app(mem_fs)
    for i in range(5):
        agent.memory.save_evolution_plan(EvolutionPlan(
            plan_id=f"p{i}", title=f"Plan {i}",
            category="capability", rationale="test", proposed_changes=[],
            files_affected=[], expected_impact="", status="approved",
            created_at=f"2025-01-0{i + 1}T00:00:00Z",
        ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/plans?n=2")
    data = r.json()
    assert len(data) == 2
    # Most recent first when paginating.
    assert data[0]["plan_id"] == "p4"
    assert data[1]["plan_id"] == "p3"


@pytest.mark.asyncio
async def test_plans_endpoint_unpaginated_newest_first(mem_fs: dict[str, str]) -> None:
    app, agent = _make_app(mem_fs)
    for i in range(5):
        agent.memory.save_evolution_plan(EvolutionPlan(
            plan_id=f"p{i}", title=f"Plan {i}",
            category="capability", rationale="test", proposed_changes=[],
            files_affected=[], expected_impact="", status="approved",
            created_at=f"2025-01-0{i + 1}T00:00:00Z",
        ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/plans")
    data = r.json()
    # No `n`: response must still be newest-first, matching the paginated branch.
    assert [p["plan_id"] for p in data] == ["p4", "p3", "p2", "p1", "p0"]


@pytest.mark.asyncio
async def test_pending_approval_surface_all_reachable_via_pagination(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 1: with more pending plans than the single-view render cap,
    every ``Memory.get_pending_plans()`` id is reachable by walking the
    paginated approval endpoint — none is silently hidden below the cut."""
    from maestro.dashboard.routes import PENDING_PLANS_PAGE_SIZE

    app, agent = _make_app(mem_fs)
    total_pending = PENDING_PLANS_PAGE_SIZE + 7
    for i in range(total_pending):
        agent.memory.save_evolution_plan(EvolutionPlan(
            plan_id=f"pend{i:03d}", title=f"Pending {i}",
            category="capability", rationale=f"rationale {i}", proposed_changes=[],
            files_affected=[], expected_impact="", status="pending_review",
            created_at=f"2025-01-01T00:{i:02d}:00Z",
        ))
    # A non-pending plan must never surface on the approval endpoint.
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="approved-x", title="Approved",
        category="capability", rationale="r", proposed_changes=[],
        files_affected=[], expected_impact="", status="approved",
        created_at="2025-01-02T00:00:00Z",
    ))

    persisted_ids = {p.plan_id for p in agent.memory.get_pending_plans()}
    assert len(persisted_ids) == total_pending

    seen: set[str] = set()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        offset = 0
        while True:
            r = await client.get(f"/api/plans/pending?offset={offset}")
            assert r.status_code == 200
            body = r.json()
            assert body["total"] == total_pending
            # No single view exceeds the render cap.
            assert len(body["plans"]) <= PENDING_PLANS_PAGE_SIZE
            for p in body["plans"]:
                seen.add(p["plan_id"])
            offset += body["count"]
            if body["count"] == 0 or offset >= body["total"]:
                break

    assert seen == persisted_ids
    assert "approved-x" not in seen


@pytest.mark.asyncio
async def test_pending_approval_surface_exposes_total(mem_fs: dict[str, str]) -> None:
    """Criterion 2: when pending count exceeds a single view, the endpoint
    exposes the full total so the not-yet-displayed remainder is discoverable
    (the "X of Y" the UI renders)."""
    from maestro.dashboard.routes import PENDING_PLANS_PAGE_SIZE

    app, agent = _make_app(mem_fs)
    total_pending = PENDING_PLANS_PAGE_SIZE + 3
    for i in range(total_pending):
        agent.memory.save_evolution_plan(EvolutionPlan(
            plan_id=f"pt{i:03d}", title=f"Pending {i}",
            category="capability", rationale=f"rationale {i}", proposed_changes=[],
            files_affected=[], expected_impact="", status="pending_review",
            created_at=f"2025-01-01T00:{i:02d}:00Z",
        ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/plans/pending")
    body = r.json()
    # First view is capped, but ``total`` reveals the hidden remainder.
    assert body["total"] == total_pending
    assert body["count"] == PENDING_PLANS_PAGE_SIZE
    assert len(body["plans"]) == PENDING_PLANS_PAGE_SIZE
    assert body["total"] > body["count"]
    # The configurable server ceiling is exposed so the client can clamp its
    # per-page ``n`` to it and never trigger the endpoint's ``le=`` 422.
    assert body["max_page_size"] == agent.config.max_page_size


@pytest.mark.asyncio
async def test_pending_offset_walk_reachable_under_lowered_max_page_size(
    mem_fs: dict[str, str],
) -> None:
    """The client's 'Show more' walks ``offset`` in fixed pages, learning the
    per-request width *reactively* from the server-reported ``max_page_size`` —
    it does NOT pre-clamp before the first request. Mirror that exact order (the
    same one ``fetchPendingWindow`` uses): the bootstrap request omits ``n``
    while the ceiling is unknown, and only after the first response reveals
    ``max_page_size`` does the client clamp its width. Run that against a server
    whose ``max_page_size`` (7) is *below* the default page size (25): the very
    first request — issued before the ceiling is known — must still respect the
    server ceiling (200, never a 422), and every plan beyond the single-page cut
    must stay reachable across the multi-page walk.

    The earlier version of this test hand-clamped ``width`` to ``server_max``
    *before* the loop and reused it on the first request, so it simulated the
    already-corrected width instead of the client's un-clamped bootstrap and
    could not have caught a first-request 422. This version keeps the width
    un-learned until the first response, so it genuinely exercises the
    reactive-clamp order the criterion claims to verify."""
    from maestro.dashboard.routes import PENDING_PLANS_PAGE_SIZE

    server_max = 7
    assert server_max < PENDING_PLANS_PAGE_SIZE  # the sub-default failing regime
    app, agent = _make_app(mem_fs, max_page_size=server_max)
    # More plans than any single page can hold, so reachability requires the
    # offset walk rather than one wide request.
    total_pending = server_max * 3 + 2
    for i in range(total_pending):
        agent.memory.save_evolution_plan(EvolutionPlan(
            plan_id=f"ow{i:03d}", title=f"Pending {i}",
            category="capability", rationale=f"rationale {i}", proposed_changes=[],
            files_affected=[], expected_impact="", status="pending_review",
            created_at=f"2025-01-01T00:{i:02d}:00Z",
        ))
    persisted_ids = {p.plan_id for p in agent.memory.get_pending_plans()}
    assert len(persisted_ids) == total_pending

    # Mirror ``fetchPendingWindow``'s reactive clamp: the width is NOT known
    # before the first request. Stay "unlearned" and omit ``n`` on the bootstrap
    # request (endpoint clamps its own default to the ceiling, so no 422); only
    # after a response reveals ``max_page_size`` does the client set its width.
    seen: set[str] = set()
    request_count = 0
    ceiling_known = False
    width = PENDING_PLANS_PAGE_SIZE  # the client default (25), still un-clamped
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        offset = 0
        while True:
            url = f"/api/plans/pending?offset={offset}"
            if ceiling_known:
                url += f"&n={min(width, PENDING_PLANS_PAGE_SIZE)}"
            r = await client.get(url)
            # The first iteration is the bootstrap (ceiling unknown, no ``n``):
            # this assertion is where a client that pre-baked ``n=25`` would 422.
            assert r.status_code == 200
            body = r.json()
            assert body["total"] == total_pending
            assert body["max_page_size"] == server_max
            assert body["count"] <= server_max
            if isinstance(body.get("max_page_size"), int) and body["max_page_size"] > 0:
                width = min(PENDING_PLANS_PAGE_SIZE, body["max_page_size"])
                ceiling_known = True
            for p in body["plans"]:
                seen.add(p["plan_id"])
            offset += body["count"]
            request_count += 1
            if body["count"] == 0 or offset >= body["total"]:
                break

    # Every plan reachable, and the walk genuinely spanned multiple pages.
    assert seen == persisted_ids
    assert request_count >= 4

    # The boundary the reactive clamp keeps the client under: an explicit ``n``
    # above ``max_page_size`` 422s, which is exactly why the client omits ``n``
    # until it learns the ceiling and then clamps to it.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        over = await client.get(f"/api/plans/pending?n={server_max + 1}")
    assert over.status_code == 422


def _fetch_pending_window_source() -> str:
    """Return the body of ``fetchPendingWindow`` from the real ``index.js``.

    ``mem_fs`` falls through to the real filesystem for source reads, so this
    inspects the actual shipped client code — no subprocess, no JS engine.
    """
    import re

    from maestro.dashboard import app as dashboard_app

    src = (dashboard_app._STATIC_DIR / "index.js").read_text(encoding="utf-8")
    match = re.search(r"async function fetchPendingWindow\(target\) \{.*?\n\}", src, re.S)
    assert match is not None, "fetchPendingWindow not found in index.js"
    return match.group(0)


@pytest.mark.asyncio
async def test_client_first_pending_request_survives_lowered_cap(
    mem_fs: dict[str, str],
) -> None:
    """The client's *bootstrap* pending request must not exceed an unknown
    ``max_page_size`` and 422.

    ``fetchPendingWindow`` learns the server ceiling only from a *successful*
    response, so its first request cannot pre-clamp ``n``. If the bootstrap
    request carries an explicit ``n`` (the old client hardcoded ``25``), an
    operator who lowers ``[dashboard] max_page_size`` below 25 makes that first
    request violate the endpoint's ``le=max_page_size`` bound: ``fetchJSON``
    throws, the ``Promise.allSettled`` pending slot rejects, ``renderPendingPlans``
    never runs, and — because the clamp never executes — ``pendingPageSize``
    stays 25 so every later refresh 422s too. The panel is permanently blank,
    silently hiding every approvable plan.

    Guard: (a) the shipped client must omit ``n`` on the bootstrap request, and
    (b) the endpoint must honour that omit-``n`` reactive-clamp walk end-to-end
    under a sub-default cap while still 422-ing the explicit oversized ``n`` the
    client must never send.
    """
    from maestro.dashboard.routes import PENDING_PLANS_PAGE_SIZE

    # (a) The real client code: the bootstrap request (issued before the ceiling
    # is known) must not embed an explicit ``n``. An unconditional
    # ``?offset=${offset}&n=`` is exactly the defect — it 422s under a lowered cap.
    body = _fetch_pending_window_source()
    assert "?offset=${offset}&n=" not in body, (
        "bootstrap request carries an unclamped n — 422s under a lowered cap"
    )
    assert "/api/plans/pending?offset=${offset}`" in body, (
        "expected a bare (n-less) bootstrap request that cannot 422"
    )

    server_max = 7
    assert server_max < PENDING_PLANS_PAGE_SIZE  # the failing regime
    app, agent = _make_app(mem_fs, max_page_size=server_max)
    total_pending = server_max * 3 + 2
    for i in range(total_pending):
        agent.memory.save_evolution_plan(EvolutionPlan(
            plan_id=f"fc{i:03d}", title=f"Pending {i}",
            category="capability", rationale=f"rationale {i}", proposed_changes=[],
            files_affected=[], expected_impact="", status="pending_review",
            created_at=f"2025-01-01T00:{i:02d}:00Z",
        ))
    persisted_ids = {p.plan_id for p in agent.memory.get_pending_plans()}
    assert len(persisted_ids) == total_pending

    # (b) Hazard the client must avoid: an explicit unclamped ``n`` (the old
    # bootstrap width) exceeds the ceiling and 422s. Then mirror the corrected
    # index.js order: the bootstrap omits ``n`` (endpoint clamps its own default
    # to the ceiling, so no 422); only after learning ``max_page_size`` does the
    # client issue width-bearing requests.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        hazard = await client.get(f"/api/plans/pending?n={PENDING_PLANS_PAGE_SIZE}")
        assert hazard.status_code == 422

        seen: set[str] = set()
        ceiling_known = False
        width = PENDING_PLANS_PAGE_SIZE
        offset = 0
        while True:
            url = f"/api/plans/pending?offset={offset}"
            if ceiling_known:
                url += f"&n={min(width, PENDING_PLANS_PAGE_SIZE)}"
            r = await client.get(url)
            # The reactive-clamp order must never send a request the endpoint 422s.
            assert r.status_code == 200
            page = r.json()
            if isinstance(page.get("max_page_size"), int) and page["max_page_size"] > 0:
                width = min(PENDING_PLANS_PAGE_SIZE, page["max_page_size"])
                ceiling_known = True
            for p in page["plans"]:
                seen.add(p["plan_id"])
            offset += page["count"]
            if page["count"] == 0 or offset >= page["total"]:
                break

    # Every pending plan stayed reachable, with no request ever 422-ing.
    assert seen == persisted_ids


@pytest.mark.asyncio
async def test_pending_bare_request_ok_under_lowered_max_page_size(
    mem_fs: dict[str, str],
) -> None:
    """A bare ``/api/plans/pending`` (client omits ``n``) must stay 200 even when
    the operator lowers ``max_page_size`` below the default page size. FastAPI
    validates the *default* against the ``le=`` bound, so a hardcoded
    ``default=PENDING_PLANS_PAGE_SIZE`` (25) would 422 the parameterless request
    whenever ``max_page_size < 25`` — breaking the endpoint's own baseline call.
    The default must be clamped to the ceiling."""
    server_max = 7
    assert server_max < 25  # below the default page size, the failing regime
    app, agent = _make_app(mem_fs, max_page_size=server_max)
    for i in range(server_max * 2):
        agent.memory.save_evolution_plan(EvolutionPlan(
            plan_id=f"br{i:03d}", title=f"Pending {i}",
            category="capability", rationale=f"rationale {i}", proposed_changes=[],
            files_affected=[], expected_impact="", status="pending_review",
            created_at=f"2025-01-01T00:{i:02d}:00Z",
        ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/plans/pending")
    assert r.status_code == 200
    body = r.json()
    # The effective page size never exceeds the server ceiling.
    assert body["page_size"] <= server_max
    assert body["count"] <= server_max
    assert body["max_page_size"] == server_max


@pytest.mark.asyncio
async def test_pending_approval_surface_content_hash_intact(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 3: the render/pagination path leaves compute_plan_content_hash
    binding intact — each rendered hash equals the persisted value, and approval
    still requires the verified hash."""
    app, agent = _make_app(mem_fs)
    for i in range(3):
        agent.memory.save_evolution_plan(EvolutionPlan(
            plan_id=f"ph{i}", title=f"Plan {i}",
            category="capability", rationale=f"rationale {i}",
            proposed_changes=["step"], files_affected=[], expected_impact="",
            status="pending_review", created_at=f"2025-01-01T00:0{i}:00Z",
        ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/plans/pending")
        body = r.json()
        # Each rendered hash matches the persisted canonical hash.
        for entry in body["plans"]:
            persisted = agent.memory.get_plan(entry["plan_id"])
            assert persisted is not None
            assert entry["plan_content_hash"] == persisted.plan_content_hash
        # Approval with the rendered hash succeeds...
        target = body["plans"][0]
        r_ok = await client.post(
            f"/api/plans/{target['plan_id']}/approve",
            json={"content_hash": target["plan_content_hash"]},
        )
        assert r_ok.status_code == 200
        # ...while a mismatched hash is still rejected — binding preserved.
        other = body["plans"][1]
        r_bad = await client.post(
            f"/api/plans/{other['plan_id']}/approve",
            json={"content_hash": "0" * 64},
        )
    assert r_bad.status_code == 409


@pytest.mark.asyncio
async def test_approve_nonexistent_plan(mem_fs: dict[str, str]) -> None:
    app, _ = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/plans/nonexistent/approve", json={"content_hash": "x"},
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_approve_already_approved(mem_fs: dict[str, str]) -> None:
    app, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="pa1", title="Already Approved",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/plans/pa1/approve", json={"content_hash": "x"},
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_approve_stale_hash_409_plan_unmutated(mem_fs: dict[str, str]) -> None:
    """A stale content hash on approve returns 409 and leaves the plan row
    untouched — the proposal-021 approve-A-implement-B race, closed."""
    app, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="pstaleh", title="Drifted",
        category="capability", rationale="original rationale",
        proposed_changes=["step 1"],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/plans/pstaleh/approve",
            json={"content_hash": "0" * 64},
        )
    assert r.status_code == 409

    plan = agent.memory.get_plan("pstaleh")
    assert plan is not None
    assert plan.status == "pending_review"
    assert plan.reviewed_at is None


@pytest.mark.asyncio
async def test_reject_stale_hash_409_plan_unmutated(mem_fs: dict[str, str]) -> None:
    """A stale content hash on reject returns 409 and leaves the plan row
    untouched — drifted rejections must not feed rejection learning."""
    app, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="pstalehr", title="Drifted Reject",
        category="capability", rationale="original rationale",
        proposed_changes=["step 1"],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/plans/pstalehr/reject",
            json={"reason": "nope", "content_hash": "0" * 64},
        )
    assert r.status_code == 409

    plan = agent.memory.get_plan("pstalehr")
    assert plan is not None
    assert plan.status == "pending_review"
    assert plan.rejection_reason is None


@pytest.mark.asyncio
async def test_approve_missing_hash_422(mem_fs: dict[str, str]) -> None:
    """A body without content_hash fails closed with 422 — a UI that drops
    the field can never approve."""
    app, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="pnohash", title="No Hash",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/plans/pnohash/approve", json={})
    assert r.status_code == 422

    plan = agent.memory.get_plan("pnohash")
    assert plan is not None
    assert plan.status == "pending_review"


@pytest.mark.asyncio
async def test_approve_extra_field_422(mem_fs: dict[str, str]) -> None:
    """extra="forbid" rejects unexpected body fields on approve."""
    app, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="pextra", title="Extra Field",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))
    saved = agent.memory.get_plan("pextra")
    assert saved is not None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/plans/pextra/approve",
            json={"content_hash": saved.plan_content_hash, "surprise": True},
        )
    assert r.status_code == 422

    plan = agent.memory.get_plan("pextra")
    assert plan is not None
    assert plan.status == "pending_review"


@pytest.mark.asyncio
async def test_plan_detail_exposes_content_hash(mem_fs: dict[str, str]) -> None:
    """GET /api/plans/{id} serialises plan_content_hash so the UI can echo it."""
    app, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="phash", title="Hash Travels",
        category="capability", rationale="test", proposed_changes=["a"],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))
    saved = agent.memory.get_plan("phash")
    assert saved is not None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/plans/phash")
    assert r.status_code == 200
    body = r.json()
    assert body["plan_content_hash"] == saved.plan_content_hash
    assert len(body["plan_content_hash"]) == 64


@pytest.mark.asyncio
async def test_tasks_endpoint(mem_fs: dict[str, str]) -> None:
    app, agent = _make_app(mem_fs)
    from maestro.memory import TaskRecord
    agent.memory.record_task(TaskRecord(
        task_id="t1", goal="Test task", plan=[],
        outcome="success", result_summary="done",
        errors=[], duration_secs=1.0,
        timestamp="2025-01-01T00:00:00Z",
    ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/tasks")
    assert r.status_code == 200
    assert len(r.json()) == 1


@pytest.mark.asyncio
async def test_versions_endpoint_empty(mem_fs: dict[str, str]) -> None:
    app, _agent = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/versions")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_versions_endpoint_returns_records(mem_fs: dict[str, str]) -> None:
    from maestro.memory import VersionRecord

    app, agent = _make_app(mem_fs)
    agent.memory.record_version(VersionRecord(
        version_id="v1", plan_id="ep_1", plan_title="first",
        commit_sha="sha1", merged_at="2026-01-01T00:00:00+00:00",
        window_tasks_total=3, window_tasks_succeeded=2, window_tasks_failed=1,
        window_sdk_cost_usd=1.0, window_sdk_calls=5,
        window_cache_hit_rate=0.5, window_rate_limit_hits=0,
        window_error_count=0, quality_gate_pass=True, notes=None,
    ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/versions?n=5")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["version_id"] == "v1"
    assert body[0]["plan_title"] == "first"
    assert body[0]["quality_gate_pass"] is True


@pytest.mark.asyncio
async def test_versions_endpoint_rejects_invalid_n(mem_fs: dict[str, str]) -> None:
    app, _agent = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/versions?n=0")
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_reject_plan(mem_fs: dict[str, str]) -> None:
    app, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="p2", title="Reject Me",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))
    saved = agent.memory.get_plan("p2")
    assert saved is not None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/plans/p2/reject",
            json={"reason": "Too risky", "content_hash": saved.plan_content_hash},
        )
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"

    # Verify it's actually rejected
    plan = agent.memory.get_plan("p2")
    assert plan is not None
    assert plan.status == "rejected"
    assert plan.rejection_reason == "Too risky"


@pytest.mark.asyncio
async def test_reject_plan_null_reason_uses_default(mem_fs: dict[str, str]) -> None:
    """Sending reason: null must store 'No reason provided', not the string 'None'."""
    app, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="pnull", title="Null Reason",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))
    saved = agent.memory.get_plan("pnull")
    assert saved is not None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/plans/pnull/reject",
            json={"reason": None, "content_hash": saved.plan_content_hash},
        )
    assert r.status_code == 200

    plan = agent.memory.get_plan("pnull")
    assert plan is not None
    assert plan.rejection_reason == "No reason provided"


@pytest.mark.asyncio
async def test_reject_plan_empty_reason_uses_default(mem_fs: dict[str, str]) -> None:
    """An empty-string reason must fall back to the default placeholder."""
    app, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="pempty", title="Empty Reason",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))
    saved = agent.memory.get_plan("pempty")
    assert saved is not None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/plans/pempty/reject",
            json={"reason": "", "content_hash": saved.plan_content_hash},
        )
    assert r.status_code == 200

    plan = agent.memory.get_plan("pempty")
    assert plan is not None
    assert plan.rejection_reason == "No reason provided"


@pytest.mark.asyncio
async def test_reject_plan_whitespace_reason_uses_default(mem_fs: dict[str, str]) -> None:
    """A whitespace-only reason must fall back to the default placeholder."""
    app, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="pws", title="Whitespace Reason",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))
    saved = agent.memory.get_plan("pws")
    assert saved is not None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/plans/pws/reject",
            json={"reason": "   ", "content_hash": saved.plan_content_hash},
        )
    assert r.status_code == 200

    plan = agent.memory.get_plan("pws")
    assert plan is not None
    assert plan.rejection_reason == "No reason provided"


@pytest.mark.asyncio
async def test_reject_plan_non_string_reason_rejected(mem_fs: dict[str, str]) -> None:
    """A non-string reason is a 400 under the strict schema (proposal 022) —
    no silent coercion, and the plan row is not mutated."""
    app, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="pnum", title="Non-string Reason",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))
    saved = agent.memory.get_plan("pnum")
    assert saved is not None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/plans/pnum/reject",
            json={"reason": 42, "content_hash": saved.plan_content_hash},
        )
    assert r.status_code == 400
    assert "reason" in r.json()["error"]

    plan = agent.memory.get_plan("pnum")
    assert plan is not None
    assert plan.status == "pending_review"
    assert plan.rejection_reason is None


@pytest.mark.asyncio
async def test_reject_plan_unknown_field_rejected(mem_fs: dict[str, str]) -> None:
    """extra="forbid": unknown body fields are a 400, not silently dropped."""
    app, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="pextra", title="Unknown Field",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))
    saved = agent.memory.get_plan("pextra")
    assert saved is not None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/plans/pextra/reject",
            json={
                "reason": "fine",
                "content_hash": saved.plan_content_hash,
                "status": "approved",
            },
        )
    assert r.status_code == 400
    assert "status" in r.json()["error"]

    plan = agent.memory.get_plan("pextra")
    assert plan is not None
    assert plan.status == "pending_review"


@pytest.mark.asyncio
async def test_reject_already_rejected(mem_fs: dict[str, str]) -> None:
    app, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="p3", title="Already Done",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        status="rejected",
        created_at="2025-01-01T00:00:00Z",
    ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/plans/p3/reject",
            json={"reason": "Again", "content_hash": "x"},
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_journal_by_type(mem_fs: dict[str, str]) -> None:
    app, agent = _make_app(mem_fs)
    agent.journal.write_thought("A reflection", "reflection", triggered_by="agent_tool")
    agent.journal.write_thought("A question", "question", triggered_by="agent_tool")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/journal?entry_type=question")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["entry_type"] == "question"


@pytest.mark.asyncio
async def test_journal_narrative(mem_fs: dict[str, str]) -> None:
    app, agent = _make_app(mem_fs)
    agent.journal.write_thought("My thought", "reflection", triggered_by="agent_tool")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/journal/narrative")
    assert r.status_code == 200
    assert "My thought" in r.json()["narrative"]


@pytest.mark.asyncio
async def test_journal_invalid_entry_type(mem_fs: dict[str, str], make_agent) -> None:  # type: ignore[no-untyped-def]
    """/api/journal should return 400 for unrecognized entry_type values."""
    from maestro.dashboard.app import create_app

    agent = make_agent()
    app = create_app(agent)
    client = TestClient(app)

    resp = client.get("/api/journal?entry_type=INVALID_TYPE")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_plan_detail(mem_fs: dict[str, str]) -> None:
    app, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="pd1", title="Detail Test",
        category="capability", rationale="test", proposed_changes=["a"],
        files_affected=["b.py"], expected_impact="c",
        created_at="2025-01-01T00:00:00Z",
    ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/plans/pd1")
    assert r.status_code == 200
    assert r.json()["title"] == "Detail Test"


@pytest.mark.asyncio
async def test_plan_detail_not_found(mem_fs: dict[str, str]) -> None:
    app, _ = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/plans/nope")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_reject_invalid_json(mem_fs: dict[str, str]) -> None:
    """Malformed JSON is a 400 under the manual strict validation (022)."""
    app, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="p4", title="Bad Body",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/plans/p4/reject",
            content="not json",
            headers={"content-type": "application/json"},
        )
    assert r.status_code == 400
    assert r.json()["error"] == "Invalid JSON body"


@pytest.mark.asyncio
async def test_approve_plan_sets_status_only(mem_fs: dict[str, str]) -> None:
    """Approving a plan only updates its status — no background implementation."""
    app, agent = _make_app(mem_fs)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="papprove", title="Approve Me",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))
    saved = agent.memory.get_plan("papprove")
    assert saved is not None

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/plans/papprove/approve",
            json={"content_hash": saved.plan_content_hash},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "approved"

    plan = agent.memory.get_plan("papprove")
    assert plan is not None
    assert plan.status == "approved"
    assert plan.reviewed_at is not None


@pytest.mark.asyncio
async def test_logs_endpoint_empty(mem_fs: dict[str, str]) -> None:
    app, _ = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/logs")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_logs_endpoint_captures_entries(mem_fs: dict[str, str]) -> None:
    app, _ = _make_app(mem_fs)
    mem_fs[str(FAKE_DIR / "logs" / "maestro.log")] = (
        "2026-04-05 10:00:00,000 [INFO] maestro: test log message alpha\n"
        "2026-04-05 10:00:01,000 [WARNING] maestro: test warning beta\n"
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/logs")
    data = r.json()
    messages = [e["message"] for e in data]
    assert "test log message alpha" in messages
    assert "test warning beta" in messages


@pytest.mark.asyncio
async def test_logs_endpoint_level_filter(mem_fs: dict[str, str]) -> None:
    app, _ = _make_app(mem_fs)
    mem_fs[str(FAKE_DIR / "logs" / "maestro.log")] = (
        "2026-04-05 10:00:00,000 [INFO] maestro: info msg\n"
        "2026-04-05 10:00:01,000 [WARNING] maestro: warn msg\n"
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/logs?level=WARNING")
    data = r.json()
    assert all(e["level"] == "WARNING" for e in data)
    messages = [e["message"] for e in data]
    assert "warn msg" in messages
    assert "info msg" not in messages


@pytest.mark.asyncio
async def test_logs_endpoint_n_param(mem_fs: dict[str, str]) -> None:
    app, _ = _make_app(mem_fs)
    lines = "".join(
        f"2026-04-05 10:00:{i:02d},000 [INFO] maestro: log {i}\n"
        for i in range(10)
    )
    mem_fs[str(FAKE_DIR / "logs" / "maestro.log")] = lines
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/logs?n=3")
    data = r.json()
    assert len(data) <= 3


@pytest.mark.asyncio
async def test_logs_html_section(mem_fs: dict[str, str]) -> None:
    app, _ = _make_app(mem_fs)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/account")
        js = await client.get("/static/index.js")
    assert "Logs" in r.text
    assert js.status_code == 200
    assert "/api/logs" in js.text


@pytest.mark.asyncio
async def test_logs_source_field(mem_fs: dict[str, str]) -> None:
    app, _ = _make_app(mem_fs)
    mem_fs[str(FAKE_DIR / "logs" / "maestro.log")] = (
        "2026-04-05 10:00:00,000 [INFO] maestro: app message\n"
        "2026-04-05 10:00:01,000 [INFO] maestro: SDK tool call: Bash\n"
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/logs")
    data = r.json()
    sdk_entries = [e for e in data if e["message"] == "SDK tool call: Bash"]
    app_entries = [e for e in data if e["message"] == "app message"]
    assert sdk_entries[0]["source"] == "sdk"
    assert app_entries[0]["source"] == "app"


@pytest.mark.asyncio
async def test_logs_source_filter(mem_fs: dict[str, str]) -> None:
    app, _ = _make_app(mem_fs)
    mem_fs[str(FAKE_DIR / "logs" / "maestro.log")] = (
        "2026-04-05 10:00:00,000 [INFO] maestro: normal app log\n"
        "2026-04-05 10:00:01,000 [INFO] maestro: SDK progress: 100 tokens\n"
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/logs?source=sdk")
    data = r.json()
    assert len(data) == 1
    assert data[0]["source"] == "sdk"


@pytest.mark.asyncio
async def test_sdk_sessions_endpoint_empty(mem_fs: dict[str, str]) -> None:

    app, _ = _make_app(mem_fs)
    with patch("maestro.logs.read_transcript_summaries", return_value=[]):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/api/logs/sdk-sessions")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_sdk_session_detail_not_found(mem_fs: dict[str, str]) -> None:

    app, _ = _make_app(mem_fs)
    with patch("maestro.logs.read_transcript_detail", return_value=None):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/api/logs/sdk-sessions/2026-03-22/nope.log")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_logs_html_has_source_filters(mem_fs: dict[str, str]) -> None:
    app, _ = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/account")
    assert "data-log-source" in r.text
    assert "sdk-sessions" in r.text


@pytest.mark.asyncio
async def test_template_escapes_plan_status_in_class_attribute(
    mem_fs: dict[str, str],
) -> None:
    """Template must use escapeHtml() for plan status in class attributes.

    This is a defense-in-depth XSS fix: even though Python types constrain
    status values, the frontend must not trust the API response to be safe
    for HTML attribute injection.
    """
    app, _ = _make_app(mem_fs)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/account")
        js = await client.get("/static/index.js")

    # Plan rendering logic lives in the extracted JS bundle now.
    assert js.status_code == 200
    html = r.text + "\n" + js.text

    # The non-pending plan rendering uses: class="plan-card ${p.status}"
    # It MUST be: class="plan-card ${escapeHtml(p.status)}"
    assert 'class="plan-card ${p.status}"' not in html, (
        "Template injects p.status into class attribute without escapeHtml() — "
        "XSS vulnerability via corrupted plan data"
    )

    # Priority is no longer rendered into a class attribute (the colour-coded
    # ``priority-<value>`` classes were dropped when the bug-first taxonomy was
    # removed). Defense-in-depth check: if priority ever returns to a class
    # context, it must be wrapped in escapeHtml().
    assert "priority-${p.priority" not in html, (
        "Template injects p.priority into class attribute without escapeHtml() — "
        "XSS vulnerability via corrupted plan data"
    )


def test_logs_endpoint_default_n_does_not_exceed_max(make_agent) -> None:  # type: ignore[no-untyped-def]
    """When log_buffer_size < 100, default n=100 must not violate Query(le=log_buffer_size)."""
    from maestro.dashboard.app import create_app

    agent = make_agent()
    agent.config.log_buffer_size = 10  # Less than the default n=100

    app = create_app(agent)
    client = TestClient(app, raise_server_exceptions=False)
    # Request without specifying n -- should use default n=100
    # but that exceeds le=10, causing a 422 error
    resp = client.get("/api/logs")
    # Should return 200 with valid data, not 422
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_cost_rates_returns_zeroed_snapshot(mem_fs: dict[str, str]) -> None:
    app, _ = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/cost-rates")
    assert r.status_code == 200
    data = r.json()
    assert data["total_calls"] == 0
    assert data["cost_per_hour"] == 0.0
    assert data["by_caller"] == {}
    assert "window_start" in data
    assert "window_end" in data


@pytest.mark.asyncio
async def test_cost_rates_respects_window_param(mem_fs: dict[str, str]) -> None:
    import json
    from datetime import UTC, datetime, timedelta
    app, agent = _make_app(mem_fs)
    cost_path = str(agent.config.cost_samples_path)

    now = datetime.now(UTC)
    recent = (now - timedelta(minutes=10)).isoformat()
    old = (now - timedelta(hours=5)).isoformat()
    lines = [
        json.dumps({
            "timestamp": old, "cost_usd": 10.0,
            "input_tokens": 5000, "output_tokens": 1000,
            "cache_read_tokens": 200, "cache_creation_tokens": 50,
            "caller": "old_call", "duration_ms": 3000,
        }),
        json.dumps({
            "timestamp": recent, "cost_usd": 1.0,
            "input_tokens": 500, "output_tokens": 100,
            "cache_read_tokens": 20, "cache_creation_tokens": 5,
            "caller": "recent_call", "duration_ms": 800,
        }),
    ]
    mem_fs[cost_path] = "\n".join(lines) + "\n"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r1 = await client.get("/api/cost-rates?window_hours=1")
        r2 = await client.get("/api/cost-rates?window_hours=24")

    assert r1.status_code == 200
    assert r1.json()["total_calls"] == 1
    assert r1.json()["total_cost_usd"] == 1.0

    assert r2.status_code == 200
    assert r2.json()["total_calls"] == 2
    assert r2.json()["total_cost_usd"] == 11.0


@pytest.mark.asyncio
async def test_budget_returns_zeroed_when_empty(mem_fs: dict[str, str]) -> None:
    app, _ = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/budget")
    assert r.status_code == 200
    data = r.json()
    assert data["total_calls"] == 0
    assert data["total_cost_usd"] == 0.0
    assert data["by_caller"] == []
    assert data["by_group"] == []
    assert "window_start" in data
    assert data["window_hours"] == 1.0


@pytest.mark.asyncio
async def test_budget_groups_by_phase(mem_fs: dict[str, str]) -> None:
    import json
    from datetime import UTC, datetime, timedelta

    app, agent = _make_app(mem_fs)
    cost_path = str(agent.config.cost_samples_path)
    now = datetime.now(UTC)
    recent = (now - timedelta(minutes=5)).isoformat()
    recent2 = (now - timedelta(minutes=10)).isoformat()
    lines = [
        json.dumps({
            "timestamp": recent, "cost_usd": 2.0,
            "input_tokens": 1000, "output_tokens": 200,
            "cache_read_tokens": 500, "cache_creation_tokens": 50,
            "caller": "reflect", "duration_ms": 800, "estimated": False,
        }),
        json.dumps({
            "timestamp": recent2, "cost_usd": 1.0,
            "input_tokens": 500, "output_tokens": 100,
            "cache_read_tokens": 250, "cache_creation_tokens": 25,
            "caller": "introspect", "duration_ms": 400, "estimated": True,
        }),
    ]
    mem_fs[cost_path] = "\n".join(lines) + "\n"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/budget?window_hours=1")
    assert r.status_code == 200
    data = r.json()
    assert data["total_calls"] == 2
    assert data["total_cost_usd"] == 3.0
    assert data["reported_cost_usd"] == 2.0
    assert data["estimated_cost_usd"] == 1.0

    caller_groups = {c["caller"]: c["group"] for c in data["by_caller"]}
    assert caller_groups == {"reflect": "Evolution", "introspect": "Contemplation"}
    group_names = {g["group"] for g in data["by_group"]}
    assert group_names == {"Evolution", "Contemplation"}


@pytest.mark.asyncio
async def test_budget_rejects_out_of_range_window(mem_fs: dict[str, str]) -> None:
    app, _ = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/budget?window_hours=0.05")
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_cost_rates_and_budget_carry_no_income(mem_fs: dict[str, str]) -> None:
    app, _ = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        rates = (await client.get("/api/cost-rates?window_hours=24")).json()
        budget = (await client.get("/api/budget?window_hours=24")).json()
    assert "income_usd_per_hour" not in rates
    assert "net_usd" not in budget



