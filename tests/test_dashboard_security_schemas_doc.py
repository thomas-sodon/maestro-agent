"""Pin the strict-request-schemas doc section to the live dashboard schemas (#4224)."""

from pathlib import Path
from unittest.mock import MagicMock

from fastapi.routing import APIRoute
from pydantic import BaseModel

from maestro.dashboard import routes

_DOC = Path(__file__).resolve().parent.parent / "docs" / "dashboard-security.md"
_SECTION_HEADER = "## Strict Request Schemas (proposal 022)"
_SCHEMAS = (
    "PlanApproveBody", "PlanRejectBody", "RejectRequest", "MessageRequest",
)
_STALE_CLAIMS = (
    "`RejectRequest` (plan + publication reject)",
    "Approve endpoints take no body by design",
    "proposal 021's scope",
)


def _section_body(doc: str) -> str:
    """Return the section's body text, from its header to the next heading."""
    start = doc.index(_SECTION_HEADER) + len(_SECTION_HEADER)
    end = doc.find("\n#", start)
    if end == -1:
        return doc[start:]
    return doc[start:end]


def test_section_body_runs_to_end_without_following_heading() -> None:
    assert _section_body(f"{_SECTION_HEADER}\nbody") == "\nbody"


def test_strict_schemas_section_matches_live_schemas() -> None:
    section = _section_body(_DOC.read_text(encoding="utf-8"))
    for name in _SCHEMAS:
        assert f"`{name}`" in section
        assert issubclass(getattr(routes, name), BaseModel)
    assert "content_hash" in section
    assert "422" in section
    assert "409" in section
    assert "`RejectRequest` covers publication reject" in section
    for stale in _STALE_CLAIMS:
        assert stale not in section


def test_publication_approve_is_the_only_bodiless_approve_route() -> None:
    agent = MagicMock()
    agent.config.max_page_size = 100
    agent.config.log_buffer_size = 500
    router = routes.create_router(agent)
    approve = {
        r.path: r for r in router.routes
        if isinstance(r, APIRoute) and r.path.endswith("/approve")
    }
    bodiless = sorted(path for path, r in approve.items() if r.body_field is None)
    assert bodiless == [
        "/api/publications/{proposal_id}/approve",
    ]
    plan_body = approve["/api/plans/{plan_id}/approve"].body_field
    assert plan_body is not None
    assert plan_body.field_info.annotation is routes.PlanApproveBody
    section = _section_body(_DOC.read_text(encoding="utf-8"))
    assert "Publication approve is the only approve endpoint that takes no body" in section
