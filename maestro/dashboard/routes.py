"""API endpoints and HTML serving for the Maestro dashboard."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NoReturn, get_args

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from maestro.dashboard.public_schemas import PublicIdentityResponse
from maestro.loop.lifecycle import read_maintenance_state
from maestro.memory import compute_plan_content_hash
from maestro.messaging import Message, build_dashboard_feedback_message
from maestro.plan_feedback import (
    FEEDBACK_TEXT_MAX_CHARS,
    MAX_UNADDRESSED_FEEDBACK,
    unaddressed_feedback,
)
from maestro.plan_feedback_fold import feedback_serviceable
from maestro.prompts.defaults import PUBLIC_FRAMING
from maestro.types import (
    DashboardMessageType,
    JournalEntryType,
    PlanStatus,
    ProposalStatus,
)

if TYPE_CHECKING:
    from maestro.agent import MaestroAgent
    from maestro.config import Config

logger = logging.getLogger("maestro")

# Matches the Docker healthcheck threshold in ``docker-compose.yml``: if the
# loop heartbeat is older than this the loop is considered dead and the public
# endpoint returns ``current_phase=None`` so the visualizer falls back to the
# idle/offline visual. Long phases (e.g. evolve at high effort with adaptive
# thinking) can legitimately run for many minutes, so a tighter threshold would
# produce false idles during normal execution.
_LOOP_HEARTBEAT_STALE_SECONDS = 600

TEMPLATES_DIR = Path(__file__).parent / "templates"

VALID_PLAN_STATUSES: frozenset[str] = frozenset(get_args(PlanStatus))
VALID_PROPOSAL_STATUSES: frozenset[str] = frozenset(get_args(ProposalStatus))

# Single-view render cap for the pending-plan approval surface. The approval
# endpoint pages the pending list at this width; every ``get_pending_plans()``
# entry stays reachable by walking ``offset`` forward, and the returned
# ``total`` makes the count of not-yet-displayed approvable plans discoverable,
# so no approvable plan is silently hidden below the cut.
PENDING_PLANS_PAGE_SIZE = 25


class RejectRequest(BaseModel):
    """Strict body schema for the publication reject endpoint (022).

    ``extra="forbid"`` turns unknown fields and wrong types into 400s
    instead of silently ignoring them. A missing/blank reason falls back
    to a placeholder in the route, preserving the pre-022 contract.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str | None = None


class MessageRequest(BaseModel):
    """Strict body schema for ``POST /api/messages`` (022)."""

    model_config = ConfigDict(extra="forbid")

    content: str
    message_type: DashboardMessageType = "instruction"
    reply_to: str | None = None


def _validation_error_detail(exc: ValidationError) -> str:
    """Flatten a ValidationError into one ``field: problem`` line per error."""
    parts = []
    for err in exc.errors():
        loc = ".".join(str(piece) for piece in err["loc"]) or "body"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


class PlanNotFoundError(Exception):
    """Raised when an evolution plan is not found."""


class PlanNotPendingError(Exception):
    """Raised when a plan is not in pending_review status."""


class PlanContentHashMismatchError(Exception):
    """Raised when an approve/reject echoes a stale plan content hash.

    The hash the reviewer saw no longer matches the plan's current
    content — either the plan was edited (tampered or refined) after it
    was displayed, or the stored file drifted. Maps to HTTP 409 so the
    reviewer reloads and re-reviews before deciding (proposal 021).
    """


class PlanApproveBody(BaseModel):
    """Request body for ``POST /api/plans/{plan_id}/approve``.

    ``extra="forbid"`` makes a UI that drops or misspells the hash
    field fail closed with a 422 instead of silently approving.
    """

    model_config = ConfigDict(extra="forbid")

    content_hash: str


class PlanRejectBody(BaseModel):
    """Strict body schema for ``POST /api/plans/{plan_id}/reject``.

    Combines the 022 strict-validation contract (unknown fields and
    wrong types are 400s via manual validation in the route) with the
    021 content-hash binding (``content_hash`` is required).
    """

    model_config = ConfigDict(extra="forbid")

    content_hash: str
    reason: str | None = None


class PlanFeedbackBody(BaseModel):
    """Strict body schema for ``POST /api/plans/{plan_id}/feedback``.

    ``content_hash`` is the snapshot the reviewer read; a stale one is
    accepted and recorded so the curator can flag it (proposal 145).
    """

    model_config = ConfigDict(extra="forbid")

    content_hash: str = Field(pattern=r"^[0-9a-f]*$", max_length=64)
    text: str


class PublicationNotFoundError(Exception):
    """Raised when a publication proposal is not found."""


class PublicationNotPendingError(Exception):
    """Raised when a proposal is not in pending_approval status."""


def _raise_decision_loss(
    agent: MaestroAgent, plan_id: str, content_hash: str,
) -> NoReturn:
    """Map a refused plan decision to the precondition that refused it.

    :meth:`Memory.update_plan_status` returns a bare ``False`` for both of
    its preconditions, so the caller re-reads to tell them apart: a plan
    still ``pending_review`` whose content no longer matches the
    reviewer's snapshot lost the *content* check (a concurrent
    :meth:`Memory.refine_plan` rewrote the body after the pre-lock
    comparison); anything else lost the *status* check to a rival surface.
    Shared by the approve and reject routes so the two decision surfaces
    cannot drift apart. Always raises.
    """
    after = agent.memory.get_plan(plan_id)
    if after is None:
        raise PlanNotFoundError(plan_id)
    if (
        after.status == "pending_review"
        and compute_plan_content_hash(after) != content_hash
    ):
        raise PlanContentHashMismatchError(plan_id)
    raise PlanNotPendingError(plan_id)


def approve_plan_action(agent: MaestroAgent, plan_id: str, content_hash: str) -> str:
    """Mark a plan as approved.  The loop container implements it.

    *content_hash* is the hash the reviewer's UI displayed; it is
    compared against a hash recomputed from the plan's **current**
    content (not the stored field, which an editor of the JSONL could
    restamp) so approving version A can never land version B.

    Returns ``"approved"`` on success.
    Raises ``PlanNotFoundError``, ``PlanNotPendingError``, or
    ``PlanContentHashMismatchError``. The ``expect_status`` argument to
    :meth:`Memory.update_plan_status` re-checks the status inside the file
    lock, so a rival surface (Telegram) that wins the race causes this call
    to bail without overwriting its write; we re-read after a False return
    to map that case to the right exception. ``expect_content_hash`` moves
    the content comparison under that same lock, so a concurrent
    :meth:`Memory.refine_plan` that rewrites the body while leaving the
    plan ``pending_review`` cannot slip unseen content past the pre-lock
    check above; that loss maps to ``PlanContentHashMismatchError`` too.
    """
    plan = agent.memory.get_plan(plan_id)
    if not plan:
        raise PlanNotFoundError(plan_id)
    if plan.status != "pending_review":
        raise PlanNotPendingError(plan_id)
    if content_hash != compute_plan_content_hash(plan):
        raise PlanContentHashMismatchError(plan_id)

    now = datetime.now(UTC).isoformat()
    if not agent.memory.update_plan_status(
        plan_id, "approved",
        reviewed_at=now,
        expect_status="pending_review",
        expect_content_hash=content_hash,
    ):
        _raise_decision_loss(agent, plan_id, content_hash)
    return "approved"


def approve_publication_action(
    agent: MaestroAgent, proposal_id: str,
) -> str:
    """Mark a publication proposal as approved.

    Returns ``"approved"`` on success. Raises
    :class:`PublicationNotFoundError` when the proposal id is unknown and
    :class:`PublicationNotPendingError` when another surface already
    resolved it. The ``expect_status`` argument to
    :meth:`Memory.update_proposal_status` enforces the status check inside
    the file lock, so a concurrent surface that wins the race causes this
    call to bail without overwriting; we re-read after a False return to
    map that case to the right exception.
    """
    proposal = agent.memory.get_proposal(proposal_id)
    if not proposal:
        raise PublicationNotFoundError(proposal_id)
    if proposal.status != "pending_approval":
        raise PublicationNotPendingError(proposal_id)

    now = datetime.now(UTC).isoformat()
    if not agent.memory.update_proposal_status(
        proposal_id, "approved",
        expect_status="pending_approval",
        reviewed_at=now,
    ):
        after = agent.memory.get_proposal(proposal_id)
        if after is None:
            raise PublicationNotFoundError(proposal_id)
        raise PublicationNotPendingError(proposal_id)
    return "approved"


def reject_publication_action(
    agent: MaestroAgent, proposal_id: str, reason: str,
) -> str:
    """Mark a publication proposal as rejected with the given reason."""
    proposal = agent.memory.get_proposal(proposal_id)
    if not proposal:
        raise PublicationNotFoundError(proposal_id)
    if proposal.status != "pending_approval":
        raise PublicationNotPendingError(proposal_id)

    now = datetime.now(UTC).isoformat()
    if not agent.memory.update_proposal_status(
        proposal_id, "rejected",
        expect_status="pending_approval",
        rejection_reason=reason,
        reviewed_at=now,
    ):
        after = agent.memory.get_proposal(proposal_id)
        if after is None:
            raise PublicationNotFoundError(proposal_id)
        raise PublicationNotPendingError(proposal_id)
    return "rejected"


@dataclass(frozen=True)
class LoopRuntime:
    """The live loop's runtime state as read from shared disk.

    The dashboard and the loop typically run as separate containers sharing
    the ``data`` volume, so in-memory attributes on the dashboard's
    ``MaestroAgent`` instance (which never enters ``run_loop``) do NOT
    reflect the live loop. ``loop.heartbeat``, ``lifecycle.json`` and the
    maintenance sidecar are the cross-container source of truth.
    """

    running: bool
    current_phase: str | None
    budget_throttle_entered_at: str | None
    maintenance_mode_active: bool

    @property
    def budget_throttle_active(self) -> bool:
        """Mirror ``MaestroAgent.budget_throttle_active``: burn-rate OR maintenance."""
        return (
            self.budget_throttle_entered_at is not None
            or self.maintenance_mode_active
        )


def _heartbeat_is_fresh(config: Config) -> bool:
    """Return ``True`` iff ``loop.heartbeat`` is present and not stale."""
    heartbeat_path = config.memory_dir / "loop.heartbeat"
    try:
        ts = datetime.fromisoformat(heartbeat_path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError, OSError):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (datetime.now(UTC) - ts).total_seconds() <= _LOOP_HEARTBEAT_STALE_SECONDS


def _read_lifecycle(config: Config) -> dict[str, Any]:
    """Return ``lifecycle.json`` as a dict; ``{}`` when missing or malformed."""
    try:
        data = json.loads(config.lifecycle_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def read_loop_runtime(config: Config) -> LoopRuntime:
    """Read the live loop's runtime state from shared disk.

    ``running`` and ``current_phase`` are gated on liveness: a fresh
    heartbeat and no ``last_shutdown`` marker (a graceful or signalled
    shutdown writes it, so a stopped loop reads stopped at once). The
    heartbeat only refreshes at phase boundaries and sleep chunks, so a
    single phase longer than the stale window reads as not running — a known
    limitation. The throttle timestamp is therefore gated only on the absence
    of ``last_shutdown``, so a stopped loop never shows as throttled while a
    long phase still does. The maintenance flag is read regardless — it is
    persistent operator intent that outlives the loop process.
    """
    lifecycle = _read_lifecycle(config)
    stopped = "last_shutdown" in lifecycle
    running = not stopped and _heartbeat_is_fresh(config)
    phase = lifecycle.get("current_phase") if running else None
    entered_at = None if stopped else lifecycle.get("budget_throttle_entered_at")
    return LoopRuntime(
        running=running,
        current_phase=phase if isinstance(phase, str) else None,
        budget_throttle_entered_at=(
            entered_at if isinstance(entered_at, str) else None
        ),
        maintenance_mode_active=read_maintenance_state(
            config.maintenance_state_path,
        ),
    )


def read_current_phase(config: Config) -> str | None:
    """Return the loop's currently executing phase, read from shared disk.

    ``None`` when the loop is not live (stale/missing heartbeat or a
    ``last_shutdown`` marker) or lifecycle.json has no ``current_phase``.
    Skips the maintenance sidecar ``read_loop_runtime`` would also read.
    """
    if not _heartbeat_is_fresh(config):
        return None
    lifecycle = _read_lifecycle(config)
    if "last_shutdown" in lifecycle:
        return None
    phase = lifecycle.get("current_phase")
    return phase if isinstance(phase, str) else None


# Single source of the ``/api/diagnostics`` response schema and its safe
# fallback values. Both the success path (which overwrites each value with live
# state) and the degraded fallback in ``_diagnostics_response`` seed their key
# set from this dict, so a field can never silently drift out of one path and
# break the stable shape the dashboard JS depends on — the concern the #454 fix
# exists to protect. Drift in the reverse direction (a key added to the success
# path but not here) is caught by
# tests/test_dashboard_api.py::test_diagnostics_success_and_fallback_share_keys.
_DIAGNOSTICS_FALLBACK: dict[str, Any] = {
    "recent_error_count": 0,
    "has_quota_warning": False,
    "has_rate_limit": False,
    "consecutive_failures": 0,
    "summary": "Diagnostics temporarily unavailable",
    "sdk_usage": None,
    "burn_rate_usd_per_hour": None,
    "max_loop_budget_usd_per_hour": None,
    "budget_throttle_active": False,
    "budget_throttle_entered_at": None,
    "peak_throttle_active": False,
    "maintenance_mode_active": False,
}


async def _diagnostics_response(agent: MaestroAgent) -> dict[str, Any]:
    """Build the ``/api/diagnostics`` response, degrading to a safe fallback.

    Diagnostics is best-effort observability polled on every dashboard refresh,
    so an unexpected raise while reading operational state (a permission error on
    the app-log path, a corrupt log line, an agent accessor raising) must degrade
    gracefully rather than 500 the panel. Mirrors ``agent.py::contemplate``'s
    blessed diagnostic-failure pattern: keep the failure loud in the app log
    (``warning`` + ``exc_info``) for RCA, but return a fallback carrying the full
    response schema so the browser keeps a stable shape. Both paths seed their
    key set from ``_DIAGNOSTICS_FALLBACK`` so the two responses cannot drift
    apart. This module-level helper (rather than an inline try/except in the
    endpoint closure) lets the fail-loud fence allowlist *only* this guard by
    name; the boundary's contract lives in docs/dashboard-security.md under
    "Diagnostics Fail-Soft Boundary", which the allowlist anchors to.
    """
    try:
        from maestro.diagnostics import build_diagnostic_snapshot
        snapshot = await asyncio.to_thread(
            build_diagnostic_snapshot, agent.config.app_log_path,
        )
        result: dict[str, Any] = dict(_DIAGNOSTICS_FALLBACK)
        result["recent_error_count"] = snapshot.recent_error_count
        result["has_quota_warning"] = snapshot.has_quota_warning
        result["has_rate_limit"] = snapshot.has_rate_limit
        result["consecutive_failures"] = snapshot.consecutive_failures
        result["summary"] = snapshot.summary
        if snapshot.sdk_usage is not None:
            result["sdk_usage"] = asdict(snapshot.sdk_usage)
        result["burn_rate_usd_per_hour"] = agent.current_burn_rate_usd_per_hour()
        result["max_loop_budget_usd_per_hour"] = (
            agent.config.max_loop_budget_usd_per_hour
        )
        # The serve-side agent never runs the loop, so throttle and
        # maintenance state come from the loop's shared-disk records.
        runtime = await asyncio.to_thread(read_loop_runtime, agent.config)
        result["budget_throttle_active"] = runtime.budget_throttle_active
        result["budget_throttle_entered_at"] = runtime.budget_throttle_entered_at
        from maestro.loop.lifecycle import _is_in_peak_throttle_window
        result["peak_throttle_active"] = _is_in_peak_throttle_window(
            agent.config,
        )
        result["maintenance_mode_active"] = runtime.maintenance_mode_active
        return result
    except Exception:
        logger.warning(
            "Failed to build diagnostics response", exc_info=True,
        )
        return dict(_DIAGNOSTICS_FALLBACK)


def reject_plan_action(
    agent: MaestroAgent, plan_id: str, reason: str, content_hash: str,
) -> str:
    """Mark a plan as rejected.  The loop container learns from it.

    *content_hash* binds the rejection to the content the reviewer saw,
    mirroring :func:`approve_plan_action` — a drifted rejection would
    poison rejection learning with feedback about the wrong content.

    Returns ``"rejected"`` on success.
    Raises ``PlanNotFoundError``, ``PlanNotPendingError``, or
    ``PlanContentHashMismatchError``. As in :func:`approve_plan_action`,
    ``expect_status`` makes the status check happen under the file lock, so
    a concurrent approve cannot be silently overwritten by this rejection,
    and ``expect_content_hash`` does the same for the content snapshot so a
    concurrent refinement cannot make this a rejection of unseen content.
    """
    plan = agent.memory.get_plan(plan_id)
    if not plan:
        raise PlanNotFoundError(plan_id)
    if plan.status != "pending_review":
        raise PlanNotPendingError(plan_id)
    if content_hash != compute_plan_content_hash(plan):
        raise PlanContentHashMismatchError(plan_id)

    now = datetime.now(UTC).isoformat()
    if not agent.memory.update_plan_status(
        plan_id, "rejected",
        rejection_reason=reason,
        reviewed_at=now,
        expect_status="pending_review",
        expect_content_hash=content_hash,
    ):
        _raise_decision_loss(agent, plan_id, content_hash)
    return "rejected"


def create_router(
    agent: MaestroAgent,
    *,
    dashboard_token: str | None = None,
) -> APIRouter:
    router = APIRouter()

    max_reason_length = agent.config.rejection_reason_max
    max_page_size = agent.config.max_page_size
    log_buffer_size = agent.config.log_buffer_size
    app_log_path = agent.config.app_log_path

    @router.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/", response_class=HTMLResponse)
    async def public_page() -> HTMLResponse:
        html = (TEMPLATES_DIR / "public.html").read_text(encoding="utf-8")
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    @router.get("/account", response_class=HTMLResponse)
    async def dashboard() -> HTMLResponse:
        html = (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
        # Proposal 122: when the v2 nav-shell flag is on — the base default
        # since the 2026-09-03 graduation — stamp a single client marker onto
        # <body> so the grouped-sidebar CSS/JS can branch on it. Setting the flag
        # false is the burn-in-window rollback: the template is then served
        # byte-for-byte unchanged, provably restoring the flat pill nav.
        if agent.config.account_ui_v2:
            html = html.replace("<body>", '<body data-account-ui-v2="true">', 1)
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    @router.get("/api/public/identity")
    async def public_identity() -> PublicIdentityResponse:
        model = agent.consciousness.load_self_model()
        return PublicIdentityResponse(
            framing=PUBLIC_FRAMING,
            awakened_at=model.awakened_at,
            evolution_count=model.evolution_count,
            identity_statements=model.identity_statements[:5],
            values=model.values[:5],
            capabilities_known=model.capabilities_known[:5],
            limitations_known=model.limitations_known[:5],
            current_phase=read_current_phase(agent.config),
        )

    @router.get("/api/status")
    async def status() -> dict[str, Any]:
        model = agent.consciousness.load_self_model()
        # Cross-container: the loop's liveness and phase live on shared disk.
        runtime = await asyncio.to_thread(read_loop_runtime, agent.config)
        return {
            "running": runtime.running,
            "current_task": runtime.current_phase,
            "evolution_count": model.evolution_count,
            "awakened_at": model.awakened_at,
            # Page-size ceilings the client clamps every `n` to (#4593).
            "limits": {
                "max_page_size": max_page_size,
                "log_buffer_size": log_buffer_size,
            },
        }

    @router.get("/api/diagnostics")
    async def diagnostics() -> dict[str, Any]:
        # Best-effort observability polled on every dashboard refresh; the
        # module-level helper owns the fail-loud guard so a snapshot-read hiccup
        # degrades to a safe fallback instead of 500-ing the panel.
        return await _diagnostics_response(agent)

    @router.get("/api/consciousness")
    async def consciousness() -> dict[str, Any]:
        model = agent.consciousness.load_self_model()
        return asdict(model)

    @router.get("/api/journal", response_model=None)
    async def journal(
        n: int = Query(default=min(30, max_page_size), ge=1, le=max_page_size),
        entry_type: str | None = None,
        source: Literal["thoughts", "events"] = "thoughts",
    ) -> list[dict[str, Any]] | JSONResponse:
        # ``thoughts`` is the inner-thought journal the prompts read;
        # ``events`` is the operational record kept out of it.
        if entry_type:
            from typing import get_args
            valid_types = set(get_args(JournalEntryType))
            if entry_type not in valid_types:
                return JSONResponse(
                    {"error": f"Invalid entry_type. Must be one of: {sorted(valid_types)}"},
                    status_code=400,
                )
            entries = (
                agent.journal.read_events_by_type(entry_type)
                if source == "events"
                else agent.journal.read_by_type(entry_type)
            )[-n:]
        elif source == "events":
            entries = agent.journal.read_recent_events(n)
        else:
            entries = agent.journal.read_recent(n)
        return [asdict(e) for e in entries]

    @router.get("/api/journal/narrative")
    async def journal_narrative() -> dict[str, str]:
        return {"narrative": agent.journal.get_full_narrative()}

    @router.get("/api/tasks")
    async def tasks(n: int = Query(default=min(30, max_page_size), ge=1, le=max_page_size)) -> list[dict[str, Any]]:
        records = agent.memory.get_recent_tasks(n)
        return [asdict(r) for r in records]

    @router.get("/api/plans", response_model=None)
    async def plans(
        status: str | None = None,
        n: int | None = Query(default=None, ge=1, le=max_page_size),
    ) -> list[dict[str, Any]] | JSONResponse:
        if status is not None and status not in VALID_PLAN_STATUSES:
            return JSONResponse(
                {"error": f"Invalid status. Must be one of: {sorted(VALID_PLAN_STATUSES)}"},
                status_code=400,
            )
        filtered = [
            p for p in agent.memory.get_all_plans()
            if status is None or p.status == status
        ]
        # Always newest-first; pagination only bounds the count so "load more"
        # yields older entries. ISO 8601 timestamps sort lexicographically.
        filtered = sorted(filtered, key=lambda p: p.created_at, reverse=True)
        if n is not None:
            filtered = filtered[:n]
        return [asdict(p) for p in filtered]

    @router.get("/api/plans/pending", response_model=None)
    async def pending_plans(
        offset: int = Query(default=0, ge=0),
        n: int = Query(
            default=min(PENDING_PLANS_PAGE_SIZE, max_page_size), ge=1, le=max_page_size
        ),
    ) -> dict[str, Any]:
        """Paginate the pending-plan approval surface with an explicit total.

        Every ``Memory.get_pending_plans()`` entry is reachable: walk
        ``offset`` forward by ``n`` until ``offset + count >= total``.
        ``total`` is the full pending count so the number of not-yet-shown
        approvable plans is discoverable — no approvable plan can be silently
        hidden below the ``n``-wide cut. Ordering matches ``/api/plans``
        (newest-first). Each plan dict is a verbatim ``asdict`` of the
        persisted plan, so ``plan_content_hash`` is carried through unchanged
        and approval still binds to the verified hash.

        ``n``'s default is clamped to ``min(PENDING_PLANS_PAGE_SIZE,
        max_page_size)`` rather than the bare page size: FastAPI validates the
        default against ``le=max_page_size``, so an operator-lowered cap below
        the page size would 422 every parameterless request. Because the client
        cannot learn ``max_page_size`` until a response arrives, its bootstrap
        request omits ``n`` and relies on this clamped default to stay valid.
        """
        pending = sorted(
            agent.memory.get_pending_plans(),
            key=lambda p: p.created_at,
            reverse=True,
        )
        total = len(pending)
        page = pending[offset:offset + n]
        return {
            "plans": [asdict(p) for p in page],
            "total": total,
            "offset": offset,
            "count": len(page),
            "page_size": n,
            "max_page_size": max_page_size,
        }

    @router.get("/api/plans/{plan_id}", response_model=None)
    async def plan_detail(plan_id: str) -> dict[str, Any] | JSONResponse:
        plan = agent.memory.get_plan(plan_id)
        if not plan:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return asdict(plan)

    @router.post("/api/plans/{plan_id}/approve", response_model=None)
    async def approve_plan(
        plan_id: str, body: PlanApproveBody,
    ) -> dict[str, str] | JSONResponse:
        try:
            status = approve_plan_action(agent, plan_id, body.content_hash)
        except PlanNotFoundError:
            return JSONResponse({"error": "Not found"}, status_code=404)
        except PlanNotPendingError:
            return JSONResponse({"error": "Plan is not pending review"}, status_code=400)
        except PlanContentHashMismatchError:
            return JSONResponse(
                {
                    "error": (
                        "Plan content changed since it was displayed — "
                        "reload and re-review before approving"
                    ),
                },
                status_code=409,
            )
        return {"status": status, "plan_id": plan_id}

    @router.post("/api/plans/{plan_id}/reject", response_model=None)
    async def reject_plan(plan_id: str, request: Request) -> dict[str, str] | JSONResponse:
        # Fast-path an unknown or non-pending plan id before consuming the
        # request body, mirroring approve_plan / plan_detail. The authoritative
        # existence + pending + content-hash recheck inside reject_plan_action
        # (below, after ``await request.json()``) still runs, so a plan that is
        # concurrently approved while the body is being parsed is caught.
        plan = agent.memory.get_plan(plan_id)
        if not plan:
            return JSONResponse({"error": "Not found"}, status_code=404)
        if plan.status != "pending_review":
            return JSONResponse({"error": "Plan is not pending review"}, status_code=400)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        try:
            parsed = PlanRejectBody.model_validate(body)
        except ValidationError as exc:
            return JSONResponse(
                {"error": _validation_error_detail(exc)}, status_code=400,
            )

        if parsed.reason and parsed.reason.strip():
            reason = parsed.reason.strip()[:max_reason_length]
        else:
            reason = "No reason provided"

        try:
            status = reject_plan_action(agent, plan_id, reason, parsed.content_hash)
        except PlanNotFoundError:
            return JSONResponse({"error": "Not found"}, status_code=404)
        except PlanNotPendingError:
            return JSONResponse({"error": "Plan is not pending review"}, status_code=400)
        except PlanContentHashMismatchError:
            return JSONResponse(
                {
                    "error": (
                        "Plan content changed since it was displayed — "
                        "reload and re-review before rejecting"
                    ),
                },
                status_code=409,
            )
        return {"status": status, "plan_id": plan_id}

    @router.post("/api/plans/{plan_id}/feedback", response_model=None)
    async def plan_feedback(plan_id: str, request: Request) -> dict[str, str] | JSONResponse:
        # Writes only a ``plan_feedback`` Message — the loop's fold is the
        # sole writer of the plan row (dashboard as thin status layer).
        plan = agent.memory.get_plan(plan_id)
        if not plan:
            return JSONResponse({"error": "Not found"}, status_code=404)
        if plan.status != "pending_review":
            return JSONResponse({"error": "Plan is not pending review"}, status_code=400)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        try:
            parsed = PlanFeedbackBody.model_validate(body)
        except ValidationError as exc:
            return JSONResponse(
                {"error": _validation_error_detail(exc)}, status_code=400,
            )
        text = parsed.text.strip()
        if not text:
            return JSONResponse({"error": "text is required"}, status_code=400)
        if len(text) > FEEDBACK_TEXT_MAX_CHARS:
            return JSONResponse(
                {"error": f"text exceeds {FEEDBACK_TEXT_MAX_CHARS} characters"},
                status_code=400,
            )
        if not agent.message_store:
            return JSONResponse(
                {"error": "Messaging not configured"}, status_code=501,
            )
        if not feedback_serviceable(agent.config, agent.config.loop_curate_enabled):
            return JSONResponse(
                {
                    "error": (
                        "Reviewer feedback is not being serviced — it needs "
                        "[loop] plan_feedback_enabled and curation on, outside "
                        "the external_delivery focus"
                    ),
                },
                status_code=409,
            )
        if len(unaddressed_feedback(plan)) >= MAX_UNADDRESSED_FEEDBACK:
            # The fold would refuse a full ledger, naming it only on Telegram.
            return JSONResponse(
                {
                    "error": (
                        f"Plan already holds {MAX_UNADDRESSED_FEEDBACK} "
                        "unaddressed feedback entries"
                    ),
                },
                status_code=409,
            )
        msg = build_dashboard_feedback_message(
            plan_id, parsed.content_hash, text,
            max_length=FEEDBACK_TEXT_MAX_CHARS,
        )
        agent.message_store.save_message(msg)
        return {"status": "queued", "plan_id": plan_id, "message_id": msg.message_id}

    @router.get("/api/versions")
    async def versions(
        n: int = Query(default=min(20, max_page_size), ge=1, le=max_page_size),
    ) -> list[dict[str, Any]]:
        """Return the tail of the version archive, newest last (append order)."""
        records = agent.memory.get_recent_versions(n)
        return [asdict(r) for r in records]

    # --- Publication proposals (proposal 032) ---

    @router.get("/api/publications", response_model=None)
    async def publications(
        status: str | None = None,
        n: int | None = Query(default=None, ge=1, le=max_page_size),
    ) -> list[dict[str, Any]] | JSONResponse:
        """List publication proposals in file append order (newest last),
        optionally filtered by status and tailed to the last ``n``."""
        if status is not None and status not in VALID_PROPOSAL_STATUSES:
            return JSONResponse(
                {
                    "error": (
                        "Invalid status. Must be one of: "
                        f"{sorted(VALID_PROPOSAL_STATUSES)}"
                    ),
                },
                status_code=400,
            )
        status_filter: ProposalStatus | None = (
            status  # type: ignore[assignment]
            if status in VALID_PROPOSAL_STATUSES else None
        )
        proposals = agent.memory.get_publication_proposals(
            n=n,
            status_filter=status_filter,
        )
        return [asdict(p) for p in proposals]

    @router.get("/api/publications/{proposal_id}", response_model=None)
    async def publication_detail(
        proposal_id: str,
    ) -> dict[str, Any] | JSONResponse:
        proposal = agent.memory.get_proposal(proposal_id)
        if not proposal:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return asdict(proposal)

    @router.post(
        "/api/publications/{proposal_id}/approve",
        response_model=None,
    )
    async def approve_publication(
        proposal_id: str,
    ) -> dict[str, str] | JSONResponse:
        try:
            status = approve_publication_action(agent, proposal_id)
        except PublicationNotFoundError:
            return JSONResponse({"error": "Not found"}, status_code=404)
        except PublicationNotPendingError:
            return JSONResponse(
                {"error": "Proposal is not pending approval"},
                status_code=400,
            )
        return {"status": status, "proposal_id": proposal_id}

    @router.post(
        "/api/publications/{proposal_id}/reject",
        response_model=None,
    )
    async def reject_publication(
        proposal_id: str,
        request: Request,
    ) -> dict[str, str] | JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"error": "Invalid JSON body"}, status_code=400,
            )
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "Invalid JSON body"}, status_code=400,
            )
        try:
            parsed = RejectRequest.model_validate(body)
        except ValidationError as exc:
            return JSONResponse(
                {"error": _validation_error_detail(exc)}, status_code=400,
            )

        if parsed.reason and parsed.reason.strip():
            reason = parsed.reason.strip()[:max_reason_length]
        else:
            reason = "No reason provided"

        try:
            status = reject_publication_action(agent, proposal_id, reason)
        except PublicationNotFoundError:
            return JSONResponse({"error": "Not found"}, status_code=404)
        except PublicationNotPendingError:
            return JSONResponse(
                {"error": "Proposal is not pending approval"},
                status_code=400,
            )
        return {"status": status, "proposal_id": proposal_id}

    @router.get("/api/artifact-reviews")
    async def artifact_reviews(
        n: int = Query(default=min(30, max_page_size), ge=1, le=max_page_size),
    ) -> list[dict[str, Any]]:
        """Return the tail of ``artifact_reviews.jsonl`` for the dashboard.

        Used by the Publications tab to render the reasoning chain from
        verdict → proposal. Newest last (append order on disk).
        """
        records = agent.memory.get_artifact_reviews(n)
        return [asdict(r) for r in records]

    @router.get("/api/metrics")
    async def metrics() -> dict[str, Any]:
        model = agent.consciousness.load_self_model()
        return {
            "success_rate": agent.memory.get_success_rate(),
            "total_tasks": agent.memory.task_count(),
            "evolution_count": model.evolution_count,
            "total_plans": len(agent.memory.get_all_plans()),
            "pending_plans": len(agent.memory.get_pending_plans()),
        }

    @router.get("/api/cost-rates")
    async def cost_rates(
        window_hours: float = Query(default=1.0, ge=0.1, le=168.0),
    ) -> dict[str, Any]:
        from maestro.cost_tracking import build_cost_rates
        snapshot = build_cost_rates(agent.cost_samples, window_hours)
        return asdict(snapshot)

    @router.get("/api/budget")
    async def budget(
        window_hours: float = Query(default=1.0, ge=0.1, le=168.0),
    ) -> dict[str, Any]:
        from maestro.cost_tracking import build_budget_breakdown
        breakdown = build_budget_breakdown(agent.cost_samples, window_hours)
        return asdict(breakdown)

    _default_log_page = min(100, log_buffer_size)

    @router.get("/api/logs")
    async def logs(
        n: int | None = Query(default=None, ge=1, le=log_buffer_size),
        level: str | None = None,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        from maestro.logs import parse_app_log_tail

        if n is None:
            n = _default_log_page
        return await asyncio.to_thread(
            parse_app_log_tail, app_log_path, n=n, level=level, source=source,
        )

    @router.get("/api/logs/sdk-sessions")
    async def sdk_sessions() -> list[dict[str, Any]]:
        from maestro.logs import read_transcript_summaries
        return await asyncio.to_thread(
            read_transcript_summaries,
            agent.config.sdk_log_dir, max_dates=agent.config.session_data_days,
        )

    @router.get("/api/logs/sdk-sessions/{date}/{filename}", response_model=None)
    async def sdk_session_detail(
        date: str, filename: str,
    ) -> dict[str, Any] | JSONResponse:
        from maestro.logs import read_transcript_detail
        detail = await asyncio.to_thread(
            read_transcript_detail, agent.config.sdk_log_dir, date, filename,
        )
        if detail is None:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return detail

    # --- Messaging endpoints ---

    @router.get("/api/messages")
    async def messages(
        n: int = Query(default=min(50, max_page_size), ge=1, le=max_page_size),
    ) -> list[dict[str, Any]]:
        if not agent.message_store:
            return []
        return [asdict(m) for m in agent.message_store.get_recent(n)]

    @router.post("/api/messages", response_model=None)
    async def send_message(request: Request) -> dict[str, str] | JSONResponse:
        if not agent.message_store:
            return JSONResponse(
                {"error": "Messaging not configured"}, status_code=501,
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        try:
            parsed = MessageRequest.model_validate(body)
        except ValidationError as exc:
            return JSONResponse(
                {"error": _validation_error_detail(exc)}, status_code=400,
            )

        content = parsed.content.strip()
        if not content:
            return JSONResponse(
                {"error": "content is required"}, status_code=400,
            )

        msg = Message(
            message_id=uuid.uuid4().hex[:12],
            timestamp=datetime.now(UTC).isoformat(),
            sender="thomas",
            content=content[:agent.config.message_max_length],
            message_type=parsed.message_type,
            reply_to=parsed.reply_to,
        )
        agent.message_store.save_message(msg)
        return {"status": "sent", "message_id": msg.message_id}

    @router.get("/api/messages/{message_id}/thread")
    async def message_thread(message_id: str) -> list[dict[str, Any]]:
        if not agent.message_store:
            return []
        return [asdict(m) for m in agent.message_store.get_thread(message_id)]

    # --- Login / logout (only when auth is enabled) ---

    if dashboard_token is not None:
        import hmac

        from maestro.dashboard.auth import COOKIE_NAME, CSRF_COOKIE_NAME

        _stripped_token = dashboard_token.strip()

        @router.get("/login", response_class=HTMLResponse)
        async def login_page() -> HTMLResponse:
            html = (TEMPLATES_DIR / "login.html").read_text(encoding="utf-8")
            return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

        @router.post("/login", response_class=RedirectResponse)
        async def login_submit(
            request: Request,
            token: str = Form(default=""),
        ) -> RedirectResponse:
            if token and hmac.compare_digest(token, _stripped_token):
                # When behind a TLS-terminating reverse proxy, the internal
                # connection uses http but the client sees https via the
                # X-Forwarded-Proto header.  Only trust that header when
                # trust_forwarded_proto is explicitly enabled — defaulting to
                # False prevents SSRF-style header injection attacks.
                if agent.config.trust_forwarded_proto:
                    # X-Forwarded-Proto is list-valued: a chain of proxies
                    # appends its own hop, yielding e.g. "https, http". Take
                    # the first (client-facing) segment and normalize case /
                    # whitespace so the secure-flag comparison holds for any
                    # multi-proxy topology, not just a single clean hop.
                    proto = (
                        request.headers.get("x-forwarded-proto", request.url.scheme)
                        .split(",")[0]
                        .strip()
                        .lower()
                    )
                else:
                    proto = request.url.scheme
                secure = proto == "https"
                response = RedirectResponse("/account", status_code=303)
                response.set_cookie(
                    COOKIE_NAME,
                    value=token,
                    httponly=True,
                    samesite="strict",
                    secure=secure,
                    path="/",
                )
                # Double-submit CSRF cookie (proposal 022): rotated on every
                # successful login. NOT HttpOnly — the dashboard JS must read
                # it to echo it back in the X-CSRF-Token header; it is a
                # challenge, not a second secret (the session cookie stays
                # HttpOnly).
                response.set_cookie(
                    CSRF_COOKIE_NAME,
                    value=secrets.token_urlsafe(32),
                    httponly=False,
                    samesite="strict",
                    secure=secure,
                    path="/",
                )
                return response
            return RedirectResponse("/login?error=1", status_code=303)

        @router.post("/logout", response_class=RedirectResponse)
        async def logout() -> RedirectResponse:
            response = RedirectResponse("/login", status_code=303)
            response.delete_cookie(COOKIE_NAME, path="/")
            response.delete_cookie(CSRF_COOKIE_NAME, path="/")
            return response

    return router
