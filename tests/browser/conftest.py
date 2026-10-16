"""Browser test fixtures: mock server, page setup, data factories."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from maestro.memory import EvolutionPlan, compute_plan_content_hash
from maestro.prompts.defaults import PUBLIC_FRAMING

DASHBOARD_DIR = Path(__file__).resolve().parents[2] / "maestro" / "dashboard"
TEMPLATES_DIR = DASHBOARD_DIR / "templates"
STATIC_DIR = DASHBOARD_DIR / "static"

# A perpetually-pending plan served by the auth mock's ``get_plan`` (any
# plan_id matches) so CSRF browser tests can drive POST
# /api/plans/{id}/approve through the production middleware and route to a
# 200. The production approve route verifies the 021 content hash, so tests
# must echo ``AUTH_STUB_PLAN_HASH`` in the request body.
AUTH_STUB_PLAN = EvolutionPlan(
    plan_id="any", title="CSRF Stub Plan",
    category="capability", rationale="csrf", proposed_changes=[],
    files_affected=[], expected_impact="",
    created_at="2025-01-01T00:00:00Z",
)
AUTH_STUB_PLAN_HASH = compute_plan_content_hash(AUTH_STUB_PLAN)


def _mount_static(app: FastAPI) -> None:
    """Serve /static/* in browser-test mock apps so extracted CSS/JS load."""
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _default_api_data() -> dict[str, Any]:
    """Return a fresh set of default mock API responses."""
    return {
        "status": {
            "running": False,
            "current_task": "",
            "evolution_count": 0,
            "awakened_at": "2025-01-01T00:00:00Z",
            "limits": {"max_page_size": 500, "log_buffer_size": 500},
        },
        "metrics": {
            "success_rate": 0.0,
            "total_tasks": 0,
            "evolution_count": 0,
            "total_plans": 0,
            "pending_plans": 0,
        },
        "consciousness": {
            "identity_statements": [],
            "values": [],
            "open_questions": [],
            "capabilities_known": [],
            "limitations_known": [],
            "evolution_count": 0,
            "awakened_at": "2025-01-01T00:00:00Z",
        },
        "plans": [],
        "journal": [],
        "journal_events": [],
        "narrative": "",
        "tasks": [],
        "logs": [],
        "sdk_sessions": [],
        "diagnostics": {
            "recent_error_count": 0,
            "has_quota_warning": False,
            "has_rate_limit": False,
            "consecutive_failures": 0,
            "summary": "",
            "sdk_usage": None,
        },
        "messages": [],
        "public_identity": {
            "framing": PUBLIC_FRAMING,
            "awakened_at": "2025-06-01T00:00:00Z",
            "evolution_count": 0,
            "identity_statements": [],
            "values": [],
            "capabilities_known": [],
            "limitations_known": [],
            "current_phase": None,
        },
        "cost_rates": {
            "window_start": "2025-06-01T11:00:00+00:00",
            "window_end": "2025-06-01T12:00:00+00:00",
            "window_seconds": 3600.0,
            "total_cost_usd": 0.0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cache_read_tokens": 0,
            "total_calls": 0,
            "cost_per_hour": 0.0,
            "cost_per_day": 0.0,
            "cost_per_month": 0.0,
            "tokens_in_per_hour": 0.0,
            "tokens_out_per_hour": 0.0,
            "by_caller": {},
        },
        "budget": {
            "window_start": "2025-06-01T11:00:00+00:00",
            "window_end": "2025-06-01T12:00:00+00:00",
            "window_hours": 1.0,
            "total_cost_usd": 0.0,
            "reported_cost_usd": 0.0,
            "estimated_cost_usd": 0.0,
            "total_calls": 0,
            "by_caller": [],
            "by_group": [],
        },
        "publications": [],
        "artifact_reviews": [],
    }


def _create_mock_app(
    data: dict[str, Any],
    post_log: list[dict[str, Any]],
) -> FastAPI:
    """Create a FastAPI app that serves real HTML but returns mock API data."""
    app = FastAPI()

    def _dashboard_html() -> HTMLResponse:
        # The dashboard hides inactive tab panels via `.tab-panel { display: none }`.
        # Most existing browser tests target elements inside multiple sections without
        # navigating between tabs, so we override that rule here to expose all panels
        # at once. Tab-navigation behaviour itself is exercised by test_tab_navigation.
        html = TEMPLATES_DIR.joinpath("index.html").read_text()
        override = "<style>.tab-panel { display: block !important; }</style>"
        html = html.replace("</head>", f"{override}</head>", 1)
        return HTMLResponse(html)

    # / and /account both serve the dashboard HTML in this mock.  Production
    # serves a public intro page at / and the dashboard at /account; keeping
    # the legacy / alias here avoids churning every browser test that calls
    # ``page.goto(url)`` directly (they pre-date the split).  The
    # ``dashboard_page`` fixture below navigates to /account so new and
    # existing page-level assertions exercise the production path.
    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return _dashboard_html()

    @app.get("/account", response_class=HTMLResponse)
    async def account() -> HTMLResponse:
        return _dashboard_html()

    @app.get("/api/public/identity")
    async def public_identity() -> dict[str, Any]:
        return data["public_identity"]

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return data["status"]

    @app.get("/api/metrics")
    async def metrics() -> dict[str, Any]:
        return data["metrics"]

    @app.get("/api/consciousness")
    async def consciousness() -> dict[str, Any]:
        return data["consciousness"]

    @app.get("/api/diagnostics")
    async def diagnostics() -> dict[str, Any]:
        return data["diagnostics"]

    @app.get("/api/plans")
    async def plans() -> list[dict[str, Any]]:
        return data["plans"]

    @app.get("/api/plans/pending")
    async def pending_plans(offset: int = 0, n: int = 25) -> dict[str, Any]:
        pending = sorted(
            (p for p in data["plans"] if p["status"] == "pending_review"),
            key=lambda p: p["created_at"],
            reverse=True,
        )
        page = pending[offset:offset + n]
        return {
            "plans": page,
            "total": len(pending),
            "offset": offset,
            "count": len(page),
            "page_size": n,
            "max_page_size": 500,
        }

    @app.get("/api/plans/{plan_id}")
    async def plan_detail(plan_id: str) -> Any:
        for p in data["plans"]:
            if p["plan_id"] == plan_id:
                return p
        return JSONResponse({"error": "Not found"}, status_code=404)

    @app.post("/api/plans/{plan_id}/approve")
    async def approve_plan(plan_id: str, request: Request) -> Any:
        for p in data["plans"]:
            if p["plan_id"] == plan_id:
                if p["status"] != "pending_review":
                    return JSONResponse({"error": "Plan is not pending review"}, status_code=400)
                body = await request.json()
                p["status"] = "approved"
                post_log.append({
                    "action": "approve",
                    "plan_id": plan_id,
                    "content_hash": str(body.get("content_hash", "")),
                    # Recorded so tests can assert the dashboard JS attaches
                    # the double-submit CSRF header (proposal 022).
                    "csrf_header": request.headers.get("x-csrf-token"),
                })
                return {"status": "approved", "plan_id": plan_id}
        return JSONResponse({"error": "Not found"}, status_code=404)

    @app.post("/api/plans/{plan_id}/reject")
    async def reject_plan(plan_id: str, request: Request) -> Any:
        for p in data["plans"]:
            if p["plan_id"] == plan_id:
                if p["status"] != "pending_review":
                    return JSONResponse({"error": "Plan is not pending review"}, status_code=400)
                body = await request.json()
                reason = str(body.get("reason", ""))[:2000]
                p["status"] = "rejected"
                p["rejection_reason"] = reason
                post_log.append({
                    "action": "reject",
                    "plan_id": plan_id,
                    "reason": reason,
                    "content_hash": str(body.get("content_hash", "")),
                })
                return {"status": "rejected", "plan_id": plan_id}
        return JSONResponse({"error": "Not found"}, status_code=404)

    @app.post("/api/plans/{plan_id}/feedback")
    async def plan_feedback(plan_id: str, request: Request) -> Any:
        body = await request.json()
        post_log.append({
            "action": "feedback",
            "plan_id": plan_id,
            "text": str(body.get("text", "")),
            "content_hash": str(body.get("content_hash", "")),
        })
        return {"status": "queued", "plan_id": plan_id, "message_id": "dashboard-feedback-x"}

    @app.get("/api/journal")
    async def journal(
        n: int = 30, entry_type: str | None = None, source: str = "thoughts",
    ) -> list[dict[str, Any]]:
        entries = data["journal_events"] if source == "events" else data["journal"]
        if entry_type:
            entries = [e for e in entries if e.get("entry_type") == entry_type]
        return entries[-n:]

    @app.get("/api/journal/narrative")
    async def journal_narrative() -> dict[str, str]:
        return {"narrative": data["narrative"]}

    @app.get("/api/tasks")
    async def tasks(n: int = 30) -> list[dict[str, Any]]:
        return data["tasks"][-n:]

    @app.get("/api/logs")
    async def logs(
        n: int = 100,
        level: str | None = None,
        source: str | None = None,
    ) -> Any:
        # Mirror the real route's ``le=log_buffer_size`` bound (#4593).
        if n > data["status"]["limits"]["log_buffer_size"]:
            return JSONResponse({"detail": "n too large"}, status_code=422)
        entries = data["logs"]
        if level:
            level_upper = level.upper()
            entries = [e for e in entries if e["level"] == level_upper]
        if source:
            entries = [e for e in entries if e.get("source") == source]
        return entries[-n:]

    @app.get("/api/logs/sdk-sessions")
    async def sdk_sessions() -> list[dict[str, Any]]:
        return data["sdk_sessions"]

    @app.get("/api/logs/sdk-sessions/{date}/{filename}")
    async def sdk_session_detail(date: str, filename: str) -> Any:
        for s in data["sdk_sessions"]:
            if s.get("date") == date and s.get("filename") == filename:
                if "turns" in s:
                    # Stamped .jsonl sidecar: return the record-shaped typed
                    # detail, mirroring maestro.logs.read_transcript_detail
                    # (asdict(record) + legacy=False) — note the detail uses the
                    # record field names (caller / session_label / task_id),
                    # unlike the summary list's phase / session / task.
                    return {
                        "caller": s.get("phase"),
                        "session_id": s.get("session_id"),
                        "started_at": s.get("started_at"),
                        "session_label": s.get("session"),
                        "iteration": s.get("iteration"),
                        "task_id": s.get("task"),
                        "turns": s["turns"],
                        "legacy": False,
                    }
                return {"date": date, "filename": filename, "content": s.get("content", "")}
        return JSONResponse({"error": "Not found"}, status_code=404)

    @app.get("/api/cost-rates")
    async def cost_rates() -> dict[str, Any]:
        return data["cost_rates"]

    @app.get("/api/budget")
    async def budget(window_hours: float = 1.0) -> dict[str, Any]:
        return data["budget"]

    @app.get("/api/versions")
    async def versions(n: int = 20) -> list[dict[str, Any]]:
        return data.get("versions", [])[-n:]

    @app.get("/api/publications")
    async def publications(
        status: str | None = None, n: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = data.get("publications", [])
        if status:
            rows = [p for p in rows if p.get("status") == status]
        if n is not None:
            rows = sorted(
                rows, key=lambda p: p.get("created_at", ""), reverse=True,
            )[:n]
        return rows

    @app.get("/api/publications/{proposal_id}")
    async def publication_detail(proposal_id: str) -> Any:
        for p in data.get("publications", []):
            if p["proposal_id"] == proposal_id:
                return p
        return JSONResponse({"error": "Not found"}, status_code=404)

    @app.post("/api/publications/{proposal_id}/approve")
    async def approve_publication(proposal_id: str) -> Any:
        for p in data.get("publications", []):
            if p["proposal_id"] == proposal_id:
                if p["status"] != "pending_approval":
                    return JSONResponse(
                        {"error": "Proposal is not pending approval"},
                        status_code=400,
                    )
                p["status"] = "approved"
                post_log.append({
                    "action": "pub_approve",
                    "proposal_id": proposal_id,
                })
                return {"status": "approved", "proposal_id": proposal_id}
        return JSONResponse({"error": "Not found"}, status_code=404)

    @app.post("/api/publications/{proposal_id}/reject")
    async def reject_publication(
        proposal_id: str, request: Request,
    ) -> Any:
        for p in data.get("publications", []):
            if p["proposal_id"] == proposal_id:
                if p["status"] != "pending_approval":
                    return JSONResponse(
                        {"error": "Proposal is not pending approval"},
                        status_code=400,
                    )
                body = await request.json()
                reason = str(body.get("reason", ""))[:2000]
                p["status"] = "rejected"
                p["rejection_reason"] = reason
                post_log.append({
                    "action": "pub_reject",
                    "proposal_id": proposal_id,
                    "reason": reason,
                })
                return {"status": "rejected", "proposal_id": proposal_id}
        return JSONResponse({"error": "Not found"}, status_code=404)

    @app.get("/api/artifact-reviews")
    async def artifact_reviews(n: int = 30) -> Any:
        # Mirror the real route's ``le=max_page_size`` bound (#4593).
        if n > data["status"]["limits"]["max_page_size"]:
            return JSONResponse({"detail": "n too large"}, status_code=422)
        return data.get("artifact_reviews", [])[-n:]

    @app.get("/api/messages")
    async def messages(n: int = 50) -> list[dict[str, Any]]:
        return data["messages"][-n:]

    @app.post("/api/messages")
    async def send_message(request: Request) -> Any:
        body = await request.json()
        content = str(body.get("content", "")).strip()
        if not content:
            return JSONResponse({"error": "content is required"}, status_code=400)
        msg = {
            "message_id": f"m{len(data['messages']) + 1}",
            "timestamp": "2025-06-01T12:00:00Z",
            "sender": "thomas",
            "content": content,
            "message_type": body.get("message_type", "instruction"),
            "reply_to": body.get("reply_to"),
            "read": False,
        }
        data["messages"].append(msg)
        post_log.append({
            "action": "send_message", "content": content,
            "message_type": msg["message_type"], "reply_to": msg["reply_to"],
        })
        return {"status": "sent", "message_id": msg["message_id"]}

    _mount_static(app)
    return app


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Data factory helpers
# ---------------------------------------------------------------------------

def make_pending_plan(
    plan_id: str = "test-plan-1",
    title: str = "Test Evolution Plan",
    **overrides: Any,
) -> dict[str, Any]:
    """Return a pending evolution plan dict suitable for mock_api_data["plans"]."""
    plan: dict[str, Any] = {
        "plan_id": plan_id,
        "title": title,
        "category": "capability",
        "priority": "capability",
        "status": "pending_review",
        "rationale": "Test rationale for plan",
        "expected_impact": "Improved testing",
        "proposed_changes": ["Change A", "Change B"],
        "files_affected": ["file.py"],
        "created_at": "2025-06-01T00:00:00Z",
        "reviewed_at": None,
        "refined_at": None,
        "rejection_reason": None,
        "obsolete_reason": None,
        "pr_url": None,
        "plan_content_hash": "a1b2c3d4e5f6" + "0" * 52,
        "notified_content_hash": "",
    }
    plan.update(overrides)
    return plan


def make_log_entry(
    message: str = "test log message",
    level: str = "INFO",
    source: str = "app",
    timestamp: str = "2025-06-01T12:00:00+00:00",
) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "level": level,
        "message": message,
        "logger": "maestro",
        "source": source,
    }


def make_journal_entry(
    content: str = "A test thought",
    entry_type: str = "reflection",
    timestamp: str = "2025-06-01T12:00:00.000000",
    triggered_by: str = "",
) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "content": content,
        "entry_type": entry_type,
        "triggered_by": triggered_by,
    }


def make_task_record(
    goal: str = "Test task goal",
    outcome: str = "success",
    duration_secs: float = 1.5,
) -> dict[str, Any]:
    return {
        "task_id": "t1",
        "goal": goal,
        "plan": [],
        "outcome": outcome,
        "result_summary": "done",
        "errors": [],
        "duration_secs": duration_secs,
        "timestamp": "2025-06-01T12:00:00Z",
    }



def make_pending_publication(
    proposal_id: str = "prop-test-1",
    proposed_name: str = "cool-tool",
    status: str = "pending_approval",
    task_id: str = "task-abc",
    proposed_readme: str | None = "# cool-tool\n\nA demo tool.",
    scrub_findings: list[str] | None = None,
    description: str = "A nice tool that does things",
    **overrides: Any,
) -> dict[str, Any]:
    """Build a publication-proposal dict for mock_api_data['publications']."""
    row: dict[str, Any] = {
        "proposal_id": proposal_id,
        "task_id": task_id,
        "review_id": f"r-{proposal_id}",
        "proposed_name": proposed_name,
        "description": description,
        "proposed_readme": proposed_readme,
        "publishable_files": ["README.md", "tool.py"],
        "status": status,
        "created_at": "2026-04-24T00:00:00+00:00",
        "reviewed_at": None,
        "rejection_reason": None,
        "scrub_findings": scrub_findings or [],
        "executing_started_at": None,
        "published_url": None,
        "error": None,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_api_data() -> dict[str, Any]:
    """Mutable dict of API responses. Modify before navigating or call refresh()."""
    return _default_api_data()


@pytest.fixture()
def post_log() -> list[dict[str, Any]]:
    """Tracks POST requests made by the browser to the mock server."""
    return []


@pytest.fixture()
def dashboard_server(
    mock_api_data: dict[str, Any],
    post_log: list[dict[str, Any]],
) -> Generator[tuple[str, dict[str, Any], list[dict[str, Any]]]]:
    """Start a real HTTP server with mock API data. Yields (url, data, post_log)."""
    app = _create_mock_app(mock_api_data, post_log)
    port = _find_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", mock_api_data, post_log
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture()
def dashboard_page(page: Any, dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]]) -> tuple[Any, str, dict[str, Any], list[dict[str, Any]]]:
    """Navigate to the dashboard (``/account``) and wait for initial render.

    Returns (page, url, data, post_log) where ``url`` is the server base URL.
    """
    url, data, post_log = dashboard_server
    page.goto(f"{url}/account")
    page.wait_for_load_state("networkidle")
    return page, url, data, post_log


def _create_public_mock_app(data: dict[str, Any]) -> FastAPI:
    """FastAPI app that serves the real ``public.html`` at / plus the identity API."""
    app = FastAPI()

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(TEMPLATES_DIR.joinpath("public.html").read_text())

    @app.get("/account", response_class=HTMLResponse)
    async def account() -> HTMLResponse:
        # Browser tests click an Account link on the public page; returning
        # a stub body is enough — these tests only verify the link destination.
        return HTMLResponse("<!doctype html><title>Account stub</title><body>ok</body>")

    @app.get("/api/public/identity")
    async def public_identity() -> dict[str, Any]:
        return data["public_identity"]

    _mount_static(app)
    return app


@pytest.fixture()
def public_page_server(
    mock_api_data: dict[str, Any],
) -> Generator[tuple[str, dict[str, Any]]]:
    """Start a mock server that serves the real public.html at /.

    Yields (url, data) — data is the same mutable dict as ``mock_api_data``.
    """
    app = _create_public_mock_app(mock_api_data)
    port = _find_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", mock_api_data
    server.should_exit = True
    thread.join(timeout=5)


def _create_auth_mock_app(
    data: dict[str, Any],
    post_log: list[dict[str, Any]],
    token: str,
) -> FastAPI:
    """Create a FastAPI app WITH authentication, using the production create_router().

    Mounts the real ``create_router()`` so that login/logout exercise the same
    code paths as production rather than a hand-rolled reimplementation.  A
    minimal agent stub satisfies the interface required by the router; the
    auth-specific browser tests (test_login.py) do not exercise API endpoints,
    so stub methods returning empty data are sufficient.
    """
    from types import SimpleNamespace

    from maestro.consciousness import SelfModel
    from maestro.dashboard.auth import BearerAuthMiddleware, CSRFMiddleware
    from maestro.dashboard.routes import create_router

    _config = SimpleNamespace(
        rejection_reason_max=2000,
        max_page_size=500,
        log_buffer_size=500,
        app_log_path=Path("/tmp/maestro.log"),
        sdk_log_dir=Path("/tmp"),
        session_data_days=10,
        message_max_length=2000,
        trust_forwarded_proto=False,
        account_ui_v2=False,
        # /api/status reads loop runtime from shared disk; nonexistent paths
        # read as a stopped loop with maintenance off.
        memory_dir=Path("/nonexistent/maestro-auth-stub"),
        lifecycle_path=Path("/nonexistent/maestro-auth-stub/lifecycle.json"),
        maintenance_state_path=Path("/nonexistent/maestro-auth-stub/maintenance.json"),
    )

    _agent: Any = SimpleNamespace(
        config=_config,
        message_store=None,
        consciousness=SimpleNamespace(
            load_self_model=lambda: SelfModel(),
        ),
        memory=SimpleNamespace(
            # AUTH_STUB_PLAN (module constant above): a perpetually-pending
            # plan so CSRF browser tests can drive a POST
            # /api/plans/{id}/approve through the production middleware to
            # a 200 (any plan_id matches).
            get_plan=lambda plan_id: AUTH_STUB_PLAN,
            update_plan_status=lambda *a, **kw: True,
            get_recent_tasks=lambda n: [],
            get_all_plans=lambda: [],
            get_pending_plans=lambda: [],
            task_count=lambda: 0,
            get_success_rate=lambda: 0.0,
            get_publication_proposals=lambda **kw: [],
            get_proposal=lambda proposal_id: None,
            update_proposal_status=lambda *a, **kw: False,
            get_artifact_reviews=lambda n: [],
            get_recent_versions=lambda n: [],
        ),
        journal=SimpleNamespace(
            read_by_type=lambda entry_type: [],
            read_recent=lambda n: [],
            get_full_narrative=lambda: "",
        ),
    )

    app = FastAPI()
    router = create_router(_agent, dashboard_token=token)  # type: ignore[arg-type]
    app.include_router(router)
    # Mirror production mount order (maestro/dashboard/app.py): CSRF added
    # first so it sits INSIDE BearerAuth and only sees authenticated requests.
    app.add_middleware(CSRFMiddleware, token=token)
    app.add_middleware(BearerAuthMiddleware, token=token)
    _mount_static(app)
    return app


AUTH_TOKEN = "browser-test-secret-42"


@pytest.fixture()
def auth_dashboard_server(
    mock_api_data: dict[str, Any],
    post_log: list[dict[str, Any]],
) -> Generator[tuple[str, dict[str, Any], list[dict[str, Any]], str]]:
    """Start a mock server with auth enabled. Yields (url, data, post_log, token)."""
    app = _create_auth_mock_app(mock_api_data, post_log, AUTH_TOKEN)
    port = _find_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", mock_api_data, post_log, AUTH_TOKEN
    server.should_exit = True
    thread.join(timeout=5)


def pytest_collection_modifyitems(items: list[Any]) -> None:
    """Auto-apply the 'browser' marker to all tests in tests/browser/."""
    for item in items:
        if "browser" in str(item.fspath):
            item.add_marker(pytest.mark.browser)
            item.add_marker(pytest.mark.timeout(60))
