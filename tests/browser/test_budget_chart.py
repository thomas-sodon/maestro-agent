"""Proof that the Budget panel draws its spend composition accessibly.

The panel used to answer "where is the money going in this window?" only with
dense tables of dollar figures — composition had to be reconstructed by
scanning a column of numbers. It now renders a ranked horizontal bar chart of
``by_group`` as inline SVG above the group table. These tests prove in a real
browser that the chart is readable by everyone, not just sighted mouse users:

* one bar per group, each carrying its own direct text value, so meaning is
  never encoded in bar length or colour alone, and bar width is a fraction of
  the largest cost in the window rather than of the total;
* the ``<svg role="img">`` resolves exactly one accessible name, and it comes
  from the *visible* composition caption via ``aria-labelledby`` — so the
  summary benefits sighted readers too instead of being hidden text;
* the group table is wrapped in a focusable region, giving a keyboard user a
  reachable data equivalent of the graphic;
* an all-unmetered window renders the honest em-dash rather than implying a
  measured ``$0.00``, and its zero-cost bars do not divide by zero;
* an empty window clears the chart to the same labelled ``.empty`` block the
  tables get, so the cold-state skeleton never shimmers forever.

The payload is injected by replacing ``data["budget"]`` wholesale before
navigating, the shape ``test_budget_tab.py::test_budget_renders_populated``
uses; the mock app force-shows every tab panel, so the Budget nav link is never
clicked. Focus is read back through ``document.activeElement`` in
``page.evaluate``, the idiom ``test_confirm_dialog.py`` uses. The directory's
``pytest_collection_modifyitems`` hook supplies the ``browser`` marker and the
60 s timeout, so neither is declared here.
"""

from __future__ import annotations

from typing import Any

# Two metered groups whose costs are 2:1 — enough to tell a bar scaled to the
# window maximum (100% / 50%) apart from one scaled to the total (67% / 33%).
_TWO_GROUP_BUDGET: dict[str, Any] = {
    "window_start": "2025-06-01T11:00:00+00:00",
    "window_end": "2025-06-01T12:00:00+00:00",
    "window_hours": 1.0,
    "total_cost_usd": 3.0,
    "reported_cost_usd": 2.0,
    "estimated_cost_usd": 1.0,
    "total_calls": 2,
    "by_caller": [
        {
            "caller": "reflect", "group": "Evolution",
            "cost_usd": 2.0, "calls": 1,
            "reported_cost_usd": 2.0, "estimated_cost_usd": 0.0,
            "share_of_total": 0.6667, "providers": ["anthropic"],
        },
        {
            "caller": "introspect", "group": "Contemplation",
            "cost_usd": 1.0, "calls": 1,
            "reported_cost_usd": 0.0, "estimated_cost_usd": 1.0,
            "share_of_total": 0.3333, "providers": ["anthropic"],
        },
    ],
    "by_group": [
        {"group": "Evolution", "cost_usd": 2.0, "calls": 1, "share_of_total": 0.6667},
        {"group": "Contemplation", "cost_usd": 1.0, "calls": 1, "share_of_total": 0.3333},
    ],
}

# A window whose only group is unmetered: the maximum cost is 0, so the
# bar-width divisor must be guarded.
_UNMETERED_BUDGET: dict[str, Any] = {
    "window_start": "2025-06-01T11:00:00+00:00",
    "window_end": "2025-06-01T12:00:00+00:00",
    "window_hours": 1.0,
    "total_cost_usd": 0.0,
    "reported_cost_usd": 0.0,
    "estimated_cost_usd": 0.0,
    "total_calls": 1,
    "by_caller": [
        {
            "caller": "reflect", "group": "Evolution",
            "cost_usd": 0.0, "calls": 1,
            "reported_cost_usd": 0.0, "estimated_cost_usd": 0.0,
            "share_of_total": 0.0, "unmetered": True, "providers": ["cli_paced"],
        },
    ],
    "by_group": [
        {
            "group": "Evolution", "cost_usd": 0.0, "calls": 1,
            "share_of_total": 0.0, "unmetered": True,
        },
    ],
}

_CAPTION = "Evolution leads at 67% of $3.00 across 2 groups"


def _open_budget(page: Any, url: str, data: dict[str, Any], budget: dict[str, Any]) -> None:
    """Serve ``budget`` from the mocked ``/api/budget`` and render the panel."""
    data["budget"] = budget
    page.goto(f"{url}/account")
    page.wait_for_load_state("networkidle")


def test_chart_draws_one_labelled_bar_per_group(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """Every group gets a bar sized against the window maximum plus a text value."""
    url, data, _ = dashboard_server
    _open_budget(page, url, data, _TWO_GROUP_BUDGET)

    bars = page.locator("#budget-group-chart rect.budget-chart-bar")
    assert bars.count() == 2, (
        "the chart must draw exactly one bar per by_group record, got "
        f"{bars.count()}"
    )
    # Widths are a fraction of the largest cost in the window (2.0), not of the
    # total (3.0) — so the leader fills the row and the runner-up is half of it.
    assert bars.nth(0).get_attribute("width") == "100.00%", (
        "the top-ranked group's bar must fill the row, being the window maximum"
    )
    assert bars.nth(1).get_attribute("width") == "50.00%", (
        "a group at half the window maximum must draw a half-width bar; a share-"
        "of-total scale would have drawn 33%"
    )

    labels = page.locator("#budget-group-chart text.budget-chart-label")
    assert labels.all_text_contents() == ["Evolution", "Contemplation"], (
        "bars must be labelled with their group names in the payload's "
        "descending-cost order, which the server already sorted"
    )

    values = page.locator("#budget-group-chart text.budget-chart-value")
    assert values.all_text_contents() == ["$2.000 · 67%", "$1.000 · 33%"], (
        "every bar must carry its own cost and share as direct text, so meaning "
        "is never encoded in bar length or colour alone"
    )


def test_label_is_clipped_to_reserve_a_value_column(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """Each label is clipped to a left column so a long name can't overrun the value.

    SVG ``<text>`` has no native ellipsis, so a long group name sharing a
    baseline with the right-anchored value would collide with it in a narrow
    panel. Every label is clipped to a reserved left column via a shared
    ``clipPath``, whose rect the stylesheet narrows to ``calc(100% - gutter)``.
    """
    url, data, _ = dashboard_server
    _open_budget(page, url, data, _TWO_GROUP_BUDGET)

    clip = page.locator("#budget-group-chart svg defs clipPath#budget-chart-label-clip")
    assert clip.count() == 1, (
        f"the chart must define exactly one label clipPath, got {clip.count()}"
    )

    labels = page.locator("#budget-group-chart text.budget-chart-label")
    label_count = labels.count()
    assert label_count == 2, f"expected one label per group, got {label_count}"
    for i in range(label_count):
        assert labels.nth(i).get_attribute("clip-path") == "url(#budget-chart-label-clip)", (
            "every label must reference the reserved-column clipPath so a long "
            "group name is clipped rather than overrunning the value text"
        )

    # The CSS calc() override must actually narrow the clip rect below the full
    # SVG width, proving a value gutter is reserved and the width="100%" fallback
    # attribute (which keeps labels visible if the stylesheet fails) did not win.
    widths = page.evaluate(
        "() => {"
        "  const svg = document.querySelector('#budget-group-chart svg');"
        "  const rect = document.querySelector('.budget-chart-label-clip-rect');"
        "  return {"
        "    svg: svg.getBoundingClientRect().width,"
        "    clip: parseFloat(getComputedStyle(rect).width),"
        "  };"
        "}"
    )
    assert widths["clip"] > 0, (
        "the clip rect must have a positive width, else labels would be clipped "
        f"to nothing; got {widths['clip']}"
    )
    reserved = widths["svg"] - widths["clip"]
    assert 120 <= reserved <= 140, (
        "the clip rect must reserve a ~130px value gutter on the right; got a "
        f"reservation of {reserved}px (svg={widths['svg']}, clip={widths['clip']})"
    )


def test_chart_takes_one_accessible_name_from_the_visible_caption(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """The ``role="img"`` graphic is named by the visible composition summary."""
    url, data, _ = dashboard_server
    _open_budget(page, url, data, _TWO_GROUP_BUDGET)

    caption = page.locator("#budget-chart-caption")
    assert caption.is_visible(), (
        "the composition summary must be visible, not hidden text — it is for "
        "sighted readers as much as it is the graphic's accessible name"
    )
    assert (caption.text_content() or "").strip() == _CAPTION, (
        "the caption must name the leading group, its share, the window total, "
        f"and the group count; got {caption.text_content()!r}"
    )

    svg = page.locator("#budget-group-chart svg[role='img']")
    assert svg.count() == 1, (
        "the chart must expose exactly one role=img graphic, got "
        f"{svg.count()}"
    )
    assert svg.get_attribute("aria-labelledby") == "budget-chart-caption", (
        "the graphic must draw its name from the visible caption rather than "
        "carrying a second, duplicate aria-label"
    )
    assert svg.get_attribute("viewBox") is None, (
        "the chart must stay viewBox-less so fluid width does not uniformly "
        "scale (and distort) its text"
    )
    # Resolved through the accessibility tree, so this proves the name actually
    # computes from aria-labelledby rather than merely that the attribute is set.
    assert page.get_by_role("img", name=_CAPTION).count() == 1, (
        "the accessibility tree must expose the graphic under the caption's text"
    )


def test_group_table_is_a_focusable_data_equivalent(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """A keyboard user can reach the same series the chart draws."""
    url, data, _ = dashboard_server
    _open_budget(page, url, data, _TWO_GROUP_BUDGET)

    region = page.locator("#budget-by-group .table-scroll")
    assert region.count() == 1, (
        "the group table must be wrapped in exactly one scroll region, got "
        f"{region.count()}"
    )
    assert region.get_attribute("role") == "region", (
        "the wrapper must expose a region role so the tab stop is announced"
    )
    assert region.get_attribute("tabindex") == "0", (
        "the wrapper must be in the tab order for a keyboard user to reach it"
    )
    assert page.locator("#budget-by-group .table-scroll table.budget-table").count() == 1, (
        "the native table must stay inside the region, carrying the identical "
        "series the chart draws"
    )
    assert page.locator("#budget-by-group table[role]").count() == 0, (
        "the native <table> already exposes its role implicitly, so no "
        "redundant role attribute may be added"
    )

    region.focus()
    active = page.evaluate(
        "() => ({"
        "  cls: document.activeElement && document.activeElement.className,"
        "  label: document.activeElement && document.activeElement.getAttribute('aria-label'),"
        "})"
    )
    assert active["cls"] == "table-scroll", (
        f"focusing the region must move activeElement to it, got {active['cls']!r}"
    )
    assert active["label"] == "Spend by phase group, data table", (
        "the focused region must announce what data it holds, got "
        f"{active['label']!r}"
    )


def test_all_unmetered_window_renders_dashes_not_zero_dollars(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """Unknown spend reads as ``—``; the zero maximum does not divide by zero."""
    url, data, _ = dashboard_server
    _open_budget(page, url, data, _UNMETERED_BUDGET)

    chart_text = page.locator("#budget-group-chart").text_content() or ""
    assert "—" in chart_text, (
        "an unmetered window's cost is unknown, so the chart must render the "
        f"honest em-dash; got {chart_text!r}"
    )
    assert "$0.00" not in chart_text, (
        f"unknown spend must never be shown as a measured $0.00; got {chart_text!r}"
    )

    bars = page.locator("#budget-group-chart rect.budget-chart-bar")
    assert bars.count() == 1, (
        f"the single unmetered group must still draw its row, got {bars.count()}"
    )
    assert bars.first.get_attribute("width") == "0.00%", (
        "with a zero window maximum the bar-width divisor is guarded, so the "
        "bar is zero-width rather than NaN from a division by zero"
    )
    assert (page.locator("#budget-chart-caption").text_content() or "").strip() == (
        "Evolution leads at 0% of — across 1 group"
    ), (
        "the caption must carry the unmetered total as an em-dash and pluralise "
        "a single group correctly"
    )


def test_empty_window_clears_the_chart_to_a_labelled_empty_state(
    dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """An idle window replaces the chart's skeleton with the labelled empty block."""
    page, *_ = dashboard_page

    empty = page.locator("#budget-group-chart .empty")
    assert empty.is_visible(), (
        "the default empty payload must clear the chart to the same labelled "
        "empty block the two tables get"
    )
    assert (empty.text_content() or "").strip() == "No spend in this window.", (
        f"the empty block must reuse the panel's wording; got {empty.text_content()!r}"
    )
    assert page.locator("#budget-group-chart .skeleton").count() == 0, (
        "the chart's cold-state skeleton must be cleared on an empty window "
        "rather than shimmering forever"
    )
    assert page.locator("#budget-group-chart svg").count() == 0, (
        "no graphic may be drawn for a window with no spend to compose"
    )
