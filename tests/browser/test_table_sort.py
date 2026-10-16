"""Proof that the Budget and Versions data tables are reorderable and accessible.

The Budget spend-by-caller table and the merged-version archive used to arrive
in a single server-determined order — an operator who wanted the caller with the
most calls or the version with the most errors had to scan a column by eye.
Activating a column header now re-sorts the rows by that column and toggles
direction on repeat activation. These tests prove in a real browser that:

* a text column sorts ascending on first activation and toggles to descending on
  the second, with the active header announcing ``aria-sort`` and every other
  sortable header reading ``none``;
* a numeric column adopts the type-appropriate default direction (descending —
  largest first) on first activation;
* the Versions archive sorts the same way;
* focus lands back on the freshly-rendered header button after the innerHTML
  swap, so keyboard operation is uninterrupted (proved via a keyboard
  activation, not just a mouse click);
* the wide by-caller and version tables are wrapped in the same focusable
  ``.table-scroll`` region the by-group table already uses, so neither overflows
  the narrow layout.

The payloads are injected by replacing ``data["budget"]`` / ``data["versions"]``
wholesale before navigating, the shape ``test_budget_chart.py`` uses; the mock
app force-shows every tab panel, so no nav link is clicked. Focus is read back
through ``document.activeElement`` in ``page.evaluate``, the idiom
``test_budget_chart.py`` / ``test_confirm_dialog.py`` use. The directory's
``pytest_collection_modifyitems`` hook supplies the ``browser`` marker and the
60 s timeout, so neither is declared here.
"""

from __future__ import annotations

from typing import Any

# Three callers whose server (default) order is cost-descending: zebra (3.0),
# mango (2.0), apple (1.0). Their names sort the opposite way alphabetically and
# their call counts (1, 2, 3) rise as cost falls, so a re-order by caller name or
# by call count is unambiguously distinguishable from the default.
_BUDGET: dict[str, Any] = {
    "window_start": "2025-06-01T11:00:00+00:00",
    "window_end": "2025-06-01T12:00:00+00:00",
    "window_hours": 1.0,
    "total_cost_usd": 6.0,
    "reported_cost_usd": 4.0,
    "estimated_cost_usd": 2.0,
    "total_calls": 6,
    "by_caller": [
        {
            "caller": "zebra", "group": "Evolution",
            "cost_usd": 3.0, "calls": 1,
            "reported_cost_usd": 3.0, "estimated_cost_usd": 0.0,
            "share_of_total": 0.5, "providers": ["anthropic"],
        },
        {
            "caller": "mango", "group": "Contemplation",
            "cost_usd": 2.0, "calls": 2,
            "reported_cost_usd": 1.0, "estimated_cost_usd": 1.0,
            "share_of_total": 0.3333, "providers": ["anthropic"],
        },
        {
            "caller": "apple", "group": "Dreaming",
            "cost_usd": 1.0, "calls": 3,
            "reported_cost_usd": 0.0, "estimated_cost_usd": 1.0,
            "share_of_total": 0.1667, "providers": ["anthropic"],
        },
    ],
    "by_group": [
        {"group": "Evolution", "cost_usd": 3.0, "calls": 1, "share_of_total": 0.5},
        {"group": "Contemplation", "cost_usd": 2.0, "calls": 2, "share_of_total": 0.3333},
        {"group": "Dreaming", "cost_usd": 1.0, "calls": 3, "share_of_total": 0.1667},
    ],
}

# Three merged versions. /api/versions returns them in array order and
# renderVersions() reverses that, so the default display order is
# newest-first: Charlie, Bravo, Alpha. Their plan names sort alphabetically the
# other way (Alpha, Bravo, Charlie) and their error counts (5, 1, 9) are all
# distinct, so a re-order is unambiguous.
_VERSIONS: list[dict[str, Any]] = [
    {
        "merged_at": "2025-06-01T10:00:00", "plan_id": "p1", "plan_title": "Alpha",
        "window_sdk_cost_usd": 1.0, "window_tasks_total": 2, "window_tasks_succeeded": 2,
        "window_error_count": 5, "quality_gate_pass": True,
    },
    {
        "merged_at": "2025-06-02T10:00:00", "plan_id": "p2", "plan_title": "Bravo",
        "window_sdk_cost_usd": 3.0, "window_tasks_total": 4, "window_tasks_succeeded": 3,
        "window_error_count": 1, "quality_gate_pass": False,
    },
    {
        "merged_at": "2025-06-03T10:00:00", "plan_id": "p3", "plan_title": "Charlie",
        "window_sdk_cost_usd": 2.0, "window_tasks_total": 1, "window_tasks_succeeded": 1,
        "window_error_count": 9, "quality_gate_pass": True,
    },
]


def _open(page: Any, url: str, data: dict[str, Any]) -> None:
    """Serve the sortable payloads and render the account dashboard."""
    data["budget"] = _BUDGET
    data["versions"] = _VERSIONS
    page.goto(f"{url}/account")
    page.wait_for_load_state("networkidle")


def _caller_names(page: Any) -> list[str]:
    """The first cell (caller name) of each by-caller data row, in DOM order."""
    return page.locator("#budget-by-caller table.budget-table td:first-child").all_text_contents()


def _plan_names(page: Any) -> list[str]:
    """The Plan cell (2nd column) of each version row, in DOM order."""
    return page.locator("#versions table.version-table tbody td:nth-child(2)").all_text_contents()


def _aria_sorts(page: Any, container: str) -> list[str]:
    """Every sortable header's aria-sort value in a container, in DOM order."""
    return page.locator(f"{container} th[aria-sort]").evaluate_all(
        "els => els.map(e => e.getAttribute('aria-sort'))",
    )


def _group_names(page: Any) -> list[str]:
    """The group cell (1st column) of each by-group data row, in DOM order."""
    return page.locator("#budget-by-group table.budget-table td:first-child").all_text_contents()


def _chart_labels(page: Any) -> list[str]:
    """The composition chart's bar labels, in draw order (the server order)."""
    return page.locator("#budget-group-chart text.budget-chart-label").all_text_contents()


def test_text_header_sorts_ascending_then_toggles_descending(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """Activating a text column sorts ascending, re-activating flips to descending."""
    url, data, _ = dashboard_server
    _open(page, url, data)

    caller_btn = page.locator('#budget-by-caller .sort-btn[data-sort-key="caller"]')
    caller_header = page.locator('#budget-by-caller th:has(.sort-btn[data-sort-key="caller"])')

    caller_btn.click()
    assert _caller_names(page) == ["apple", "mango", "zebra"], (
        "activating the Caller header must sort the rows ascending by caller name, "
        "not leave the server's cost-descending order"
    )
    assert caller_header.get_attribute("aria-sort") == "ascending", (
        "a text column defaults to ascending on first activation and must announce it"
    )
    # Every other sortable header must read none — only one column is active.
    other = [v for v in _aria_sorts(page, "#budget-by-caller") if v != "ascending"]
    assert other == ["none"] * 6, (
        "every non-active sortable header must read aria-sort=none; got "
        f"{_aria_sorts(page, '#budget-by-caller')!r}"
    )

    caller_btn.click()
    assert _caller_names(page) == ["zebra", "mango", "apple"], (
        "re-activating the same header must flip the order to descending"
    )
    assert caller_header.get_attribute("aria-sort") == "descending", (
        "re-activation must toggle the announced direction to descending"
    )


def test_numeric_header_defaults_to_descending(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """A numeric column sorts descending (largest first) on first activation."""
    url, data, _ = dashboard_server
    _open(page, url, data)

    calls_btn = page.locator('#budget-by-caller .sort-btn[data-sort-key="calls"]')
    calls_header = page.locator('#budget-by-caller th:has(.sort-btn[data-sort-key="calls"])')

    calls_btn.click()
    # Call counts are 1/2/3 for zebra/mango/apple, so descending by calls surfaces
    # apple (3) first — a type-appropriate default of descending, not ascending.
    assert _caller_names(page) == ["apple", "mango", "zebra"], (
        "a numeric column must default to descending, surfacing the largest first"
    )
    assert calls_header.get_attribute("aria-sort") == "descending", (
        "the numeric column's first activation must announce descending"
    )


def test_versions_header_sorts_and_wraps_in_scroll_region(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """The version archive sorts by a column and lives in a focusable region."""
    url, data, _ = dashboard_server
    _open(page, url, data)

    region = page.locator("#versions .table-scroll")
    region.wait_for(state="attached")
    assert region.count() == 1, (
        f"the version table must be wrapped in exactly one scroll region, got {region.count()}"
    )
    assert region.get_attribute("role") == "region", (
        "the wrapper must expose a region role so the tab stop is announced"
    )
    assert region.get_attribute("tabindex") == "0", (
        "the wrapper must be in the tab order for a keyboard user to reach it"
    )
    assert region.get_attribute("aria-label") == "Version archive, data table", (
        "the focused region must announce what data it holds"
    )
    assert page.locator("#versions .table-scroll table.version-table").count() == 1, (
        "the native table must stay inside the region"
    )

    plan_btn = page.locator('#versions .sort-btn[data-sort-key="plan"]')
    plan_header = page.locator('#versions th:has(.sort-btn[data-sort-key="plan"])')

    plan_btn.click()
    assert _plan_names(page) == ["Alpha", "Bravo", "Charlie"], (
        "activating the Plan header must sort ascending by plan name, replacing the "
        "default newest-first order"
    )
    assert plan_header.get_attribute("aria-sort") == "ascending", (
        "the Plan column must announce its ascending sort"
    )

    plan_btn.click()
    assert _plan_names(page) == ["Charlie", "Bravo", "Alpha"], (
        "re-activating must flip the version order to descending"
    )
    assert plan_header.get_attribute("aria-sort") == "descending", (
        "re-activation must toggle the announced direction to descending"
    )


def test_caller_table_is_wrapped_in_a_scroll_region(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """The wide by-caller table gains the same focusable scroll region as by-group."""
    url, data, _ = dashboard_server
    _open(page, url, data)

    region = page.locator("#budget-by-caller .table-scroll")
    region.wait_for(state="attached")
    assert region.count() == 1, (
        f"the by-caller table must be wrapped in exactly one scroll region, got {region.count()}"
    )
    assert region.get_attribute("role") == "region", (
        "the wrapper must expose a region role so the tab stop is announced"
    )
    assert region.get_attribute("tabindex") == "0", (
        "the wrapper must be in the tab order for a keyboard user to reach it"
    )
    assert region.get_attribute("aria-label") == "Spend by caller, data table", (
        "the focused region must announce what data it holds"
    )
    assert page.locator("#budget-by-caller table[role]").count() == 0, (
        "the native <table> already exposes its role implicitly, so no redundant "
        "role attribute may be added"
    )


def test_keyboard_activation_sorts_and_restores_focus(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """Enter on a focused header sorts and leaves focus on the freshly-rendered button.

    The render swaps the table's innerHTML, destroying the button that was
    activated; the handler must move focus onto its replacement so keyboard
    operation is not dropped to the document body.
    """
    url, data, _ = dashboard_server
    _open(page, url, data)

    caller_btn = page.locator('#budget-by-caller .sort-btn[data-sort-key="caller"]')
    caller_btn.wait_for(state="attached")
    caller_btn.focus()
    page.keyboard.press("Enter")

    assert _caller_names(page) == ["apple", "mango", "zebra"], (
        "keyboard activation must sort the table exactly as a mouse click does"
    )
    # Read the post-render activeElement: proving focus was restored onto the new
    # button (which now carries the `active` class), not left on a detached node
    # or dropped to <body>.
    active = page.evaluate(
        "() => ({"
        "  cls: document.activeElement && document.activeElement.className,"
        "  table: document.activeElement && document.activeElement.getAttribute('data-sort-table'),"
        "  key: document.activeElement && document.activeElement.getAttribute('data-sort-key'),"
        "})"
    )
    assert active["table"] == "budgetCaller" and active["key"] == "caller", (
        "focus must land back on the same column's header button after the "
        f"innerHTML swap, got {active!r}"
    )
    assert "active" in (active["cls"] or ""), (
        "the focused button must be the freshly-rendered active one, not a stale "
        f"pre-render node, got className {active['cls']!r}"
    )


def test_group_header_sorts_table_while_chart_keeps_server_order(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """Sorting the by-group table leaves the composition chart in server order.

    ``renderBudget`` draws the chart from the untouched ``b.by_group`` and sorts
    only a copy for the table (``sortRows`` never mutates its input), so
    activating a group-table header must reorder the table without disturbing the
    chart above it — the invariant the ``renderBudget`` comment claims.
    """
    url, data, _ = dashboard_server
    _open(page, url, data)

    # Both the chart and the table start in the server's cost-descending order:
    # Evolution (3.0), Contemplation (2.0), Dreaming (1.0).
    assert _group_names(page) == ["Evolution", "Contemplation", "Dreaming"], (
        "the by-group table must start in the server's cost-descending order"
    )
    assert _chart_labels(page) == ["Evolution", "Contemplation", "Dreaming"], (
        "the composition chart must start in the server's cost-descending order"
    )

    group_btn = page.locator('#budget-by-group .sort-btn[data-sort-key="group"]')
    group_header = page.locator('#budget-by-group th:has(.sort-btn[data-sort-key="group"])')

    group_btn.click()
    # A text column sorts ascending by name, distinct from the server order.
    assert _group_names(page) == ["Contemplation", "Dreaming", "Evolution"], (
        "activating the Group header must sort the table ascending by group name"
    )
    assert group_header.get_attribute("aria-sort") == "ascending", (
        "the active group column must announce its ascending sort"
    )
    # The chart reads the untouched server array, so it stays cost-descending
    # even though the table beneath it is now name-ascending.
    assert _chart_labels(page) == ["Evolution", "Contemplation", "Dreaming"], (
        "the composition chart must keep the server order while the group table "
        "sorts — renderGroupChart reads b.by_group, which sortRows never mutates"
    )
