"""Static guard: the dashboard client sends no unclamped page size (#4593)."""

from __future__ import annotations

import re
from pathlib import Path

INDEX_JS = Path(__file__).resolve().parent.parent / "maestro" / "dashboard" / "static" / "index.js"

# A hardcoded ``n=<digits>`` in a URL, or any ``n=${...}`` interpolation; the
# interpolations below are the known-clamped sites and are allowlisted.
_UNCLAMPED = re.compile(r"[?&]n=(?:\d|\$\{[^}]*\})")

_CLAMPED_SITES = frozenset({
    "?n=${n}",  # withPageSize's own template; intake/quotes min(..., max_page_size)
    "&n=${n}",  # quotes pending_approval, same clamped ``n``
    "&n=${Math.min(pendingPageSize, target - offset)}",  # pending walk
})


def _violations(source: str) -> list[str]:
    return [m.group(0) for m in _UNCLAMPED.finditer(source) if m.group(0) not in _CLAMPED_SITES]


def test_index_js_sends_no_unclamped_page_size() -> None:
    source = INDEX_JS.read_text(encoding="utf-8")
    assert _violations(source) == []


def test_index_js_routes_page_sizes_through_helper() -> None:
    source = INDEX_JS.read_text(encoding="utf-8")
    assert "function clampPageSize(" in source
    assert "withPageSize('/api/logs', 200, 'log_buffer_size')" in source
    assert "withPageSize('/api/artifact-reviews', 50, 'max_page_size')" in source


def test_guard_detects_seeded_violation() -> None:
    seeded = (
        "fetchJSON('/api/logs?n=200'); fetchJSON(`/api/tasks?n=${pageSize.tasks}`);"
        " fetchJSON(`/api/x?n=${limit}`); fetchJSON(`/api/y?n=${n}`);"
    )
    assert _violations(seeded) == ["?n=2", "?n=${pageSize.tasks}", "?n=${limit}"]
