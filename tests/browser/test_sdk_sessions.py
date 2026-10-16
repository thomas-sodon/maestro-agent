"""Tests for the SDK sessions browsing feature."""

from __future__ import annotations

from typing import Any


def _turn(kind: str, role: str, content: str) -> dict[str, Any]:
    """One typed transcript turn in the record shape the reader emits."""
    return {"kind": kind, "role": role, "ts": "2025-06-01T10:00:00Z", "content": content}


def _stamped(
    filename: str,
    phase: str,
    *,
    turns: list[dict[str, Any]],
    session: str = "A",
    iteration: int = 1,
    task: str = "task-1",
    date: str = "2025-06-01",
) -> dict[str, Any]:
    """A provenance-rich summary dict carrying a ``turns`` key.

    Mirrors ``read_transcript_summaries``: the summary uses phase / session /
    task field names, and the ``turns`` key marks the session as stamped so the
    mock detail route returns the typed (non-legacy) shape.
    """
    return {
        "date": date,
        "filename": filename,
        "size_bytes": 2048,
        "phase": phase,
        "session": session,
        "iteration": iteration,
        "task": task,
        "turn_count": len(turns),
        "started_at": "2025-06-01T10:00:00Z",
        "session_id": "deadbeef",
        "turns": turns,
    }


def _open_sessions(page: Any) -> None:
    """Activate the Sessions tab and wait for its first fetch to paint."""
    page.locator('.nav-link[data-nav="sessions"]').click()
    page.wait_for_timeout(500)


def test_sessions_tab_is_a_nav_destination(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """The transcript view is a first-class tab, not a control buried in Logs.

    The mock fixture force-shows every ``.tab-panel``, so panel *hiding* is
    covered by test_tab_navigation; what this pins is that Sessions owns a nav
    link and a panel of its own, and that activating it populates both panes
    without any further click.
    """
    url, data, _ = dashboard_server
    data["sdk_sessions"] = [
        _stamped("session-001.jsonl", "contemplate", turns=[_turn("text", "assistant", "thinking out loud")]),
    ]
    page.goto(url)
    page.wait_for_load_state("networkidle")

    assert page.locator('.nav-link[data-nav="sessions"]').count() == 1
    assert page.locator('.tab-panel[data-tab="sessions"] #sdk-sessions-panel').count() == 1
    # The Logs tab no longer owns the session panels.
    assert page.locator('.tab-panel[data-tab="logs"] #sdk-sessions-panel').count() == 0

    _open_sessions(page)
    # Activation alone fetches the list and, while following, the newest
    # transcript — no second click needed.
    assert page.locator(".sdk-session-item").count() == 1
    assert "thinking out loud" in (page.locator("#sdk-session-content").text_content() or "")


def test_follow_auto_selects_newest_and_click_pauses(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """Following tails the newest session; picking one by hand pauses the tail."""
    url, data, _ = dashboard_server
    data["sdk_sessions"] = [
        _stamped("newest.jsonl", "contemplate", turns=[_turn("text", "assistant", "NEWEST-TURN")]),
        _stamped("older.jsonl", "evolve", turns=[_turn("text", "assistant", "OLDER-TURN")]),
    ]
    page.goto(url)
    page.wait_for_load_state("networkidle")
    _open_sessions(page)

    follow = page.locator("#btn-sdk-follow")
    assert follow.get_attribute("aria-pressed") == "true"
    content = page.locator("#sdk-session-content")
    assert "NEWEST-TURN" in (content.text_content() or "")
    # The followed session is marked in the list.
    assert page.locator(".sdk-session-item.selected").count() == 1

    # Clicking the older session stops the tail so a poll cannot yank it back.
    page.locator(".sdk-session-item").nth(1).click()
    page.wait_for_timeout(300)
    assert follow.get_attribute("aria-pressed") == "false"
    assert "OLDER-TURN" in (content.text_content() or "")

    # Re-enabling follow returns to the newest session.
    follow.click()
    page.wait_for_timeout(400)
    assert follow.get_attribute("aria-pressed") == "true"
    assert "NEWEST-TURN" in (content.text_content() or "")


def test_sessions_empty_state(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["sdk_sessions"] = []
    page.goto(url)
    page.wait_for_load_state("networkidle")

    _open_sessions(page)

    panel = page.locator("#sdk-sessions-panel")
    assert "No SDK sessions recorded yet." in (panel.text_content() or "")


def test_sessions_list_rendered(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["sdk_sessions"] = [
        {"date": "2025-06-01", "filename": "session-001.jsonl", "size_bytes": 2048, "content": "log content"},
        {"date": "2025-06-01", "filename": "session-002.jsonl", "size_bytes": 4096, "content": "more logs"},
        {"date": "2025-06-02", "filename": "session-003.jsonl", "size_bytes": 1024, "content": "another day"},
    ]
    page.goto(url)
    page.wait_for_load_state("networkidle")

    _open_sessions(page)

    # Should show session items
    items = page.locator(".sdk-session-item")
    assert items.count() == 3

    # Should show date headers
    panel_text = page.locator("#sdk-sessions-panel").text_content() or ""
    assert "2025-06-01" in panel_text
    assert "2025-06-02" in panel_text

    # Should show filenames
    assert "session-001.jsonl" in panel_text
    assert "session-002.jsonl" in panel_text
    assert "session-003.jsonl" in panel_text


def test_session_detail_loaded_on_click(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["sdk_sessions"] = [
        {"date": "2025-06-01", "filename": "session-001.jsonl", "size_bytes": 2048, "content": "detailed log content here"},
    ]
    page.goto(url)
    page.wait_for_load_state("networkidle")

    _open_sessions(page)

    # Click the session item
    page.locator(".sdk-session-item").first.click()
    page.wait_for_timeout(500)

    # Session content should be visible
    content_el = page.locator("#sdk-session-content")
    assert content_el.is_visible()
    assert "detailed log content here" in (content_el.text_content() or "")


def test_stamped_session_detail_renders_turn_blocks(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["sdk_sessions"] = [
        _stamped(
            "10-00-00_contemplate_abc.jsonl",
            "contemplate",
            turns=[
                _turn("prompt", "user", "the seed prompt"),
                _turn("thinking", "assistant", "internal reasoning"),
                _turn("text", "assistant", "the final answer"),
            ],
        ),
    ]
    page.goto(url)
    page.wait_for_load_state("networkidle")

    _open_sessions(page)
    page.locator(".sdk-session-item").first.click()
    page.wait_for_timeout(500)

    content = page.locator("#sdk-session-content")
    assert content.is_visible()

    # Distinct typed-turn blocks, one per turn — not a single raw blob.
    assert page.locator(".sdk-turn").count() == 3

    # Provenance header carries the phase.
    header_text = page.locator(".sdk-prov-header").text_content() or ""
    assert "contemplate" in header_text

    content_text = content.text_content() or ""
    assert "the seed prompt" in content_text
    assert "internal reasoning" in content_text
    assert "the final answer" in content_text
    # The streaming per-chunk noise the record collapses must not appear.
    assert "Text delta:" not in content_text


def test_sessions_list_shows_phase_and_filters(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["sdk_sessions"] = [
        _stamped("10-00-00_contemplate_a.jsonl", "contemplate", turns=[_turn("text", "assistant", "one")]),
        _stamped("10-01-00_evolve_b.jsonl", "evolve", turns=[_turn("text", "assistant", "two")]),
    ]
    page.goto(url)
    page.wait_for_load_state("networkidle")

    _open_sessions(page)

    # The list is labelled by phase, not just filename.
    panel_text = page.locator("#sdk-sessions-panel").text_content() or ""
    assert "contemplate" in panel_text
    assert "evolve" in panel_text
    assert page.locator(".sdk-session-item").count() == 2

    # Phase-filter buttons exist and "All" is active by default.
    assert page.locator('[data-sdk-phase="contemplate"]').count() == 1
    all_btn = page.locator('[data-sdk-phase=""]')
    assert "active" in (all_btn.get_attribute("class") or "")

    # Filtering to one phase narrows the list…
    page.locator('[data-sdk-phase="contemplate"]').click()
    page.wait_for_timeout(300)
    assert page.locator(".sdk-session-item").count() == 1

    # …and honours the single-active invariant.
    contemplate_btn = page.locator('[data-sdk-phase="contemplate"]')
    assert "active" in (contemplate_btn.get_attribute("class") or "")
    assert "active" not in (page.locator('[data-sdk-phase=""]').get_attribute("class") or "")
    assert page.locator("[data-sdk-phase].active").count() == 1


def test_stale_phase_filter_recovers_when_phase_ages_out(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """A persisted phase filter whose phase later vanishes must not strand the
    list blank.

    filterState.sdkPhase persists across renders. If the user filters to a phase
    and a later fetch yields a summary set with no non-null phases (the stamped
    sessions aged out, or the writer flag was turned off, leaving only legacy
    .log sessions), the phase-button row — including the "All" reset button —
    renders nothing, so filtering to the now-absent phase would leave an
    unrecoverable empty panel. The render must instead fall back to showing every
    session, without mutating the persisted filter.
    """
    url, data, _ = dashboard_server
    legacy = {
        "date": "2025-06-01",
        "filename": "10-05-00_legacy.log",
        "size_bytes": 128,
        "content": "raw blob",
    }
    data["sdk_sessions"] = [
        _stamped("10-00-00_act_a.jsonl", "act", turns=[_turn("text", "assistant", "one")]),
        legacy,
    ]
    page.goto(url)
    page.wait_for_load_state("networkidle")

    _open_sessions(page)

    # Filter to the "act" phase — persists in filterState.sdkPhase.
    page.locator('[data-sdk-phase="act"]').click()
    page.wait_for_timeout(300)
    assert page.locator(".sdk-session-item").count() == 1

    # The stamped "act" session ages out; only the phase-less legacy .log
    # remains, so the next fetch produces a summary set with no non-null phases.
    data["sdk_sessions"] = [legacy]
    page.evaluate("() => loadSdkSessions()")
    page.wait_for_timeout(300)

    # No phase buttons can render (no non-null phases), so there is no "All"
    # reset button…
    assert page.locator("[data-sdk-phase]").count() == 0
    # …but the list must NOT be stranded blank: the stale "act" filter falls back
    # to showing every session rather than filtering to nothing.
    assert page.locator(".sdk-session-item").count() == 1
    panel_text = page.locator("#sdk-sessions-panel").text_content() or ""
    assert "10-05-00_legacy.log" in panel_text


def test_legacy_session_renders_raw_blob(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    # A label-less .log (no turns key) — the mock returns the legacy shape.
    data["sdk_sessions"] = [
        {
            "date": "2025-06-01",
            "filename": "10-00-00_abc.log",
            "size_bytes": 128,
            "content": "raw blob line one\nText delta: streaming noise\nraw blob line two",
        },
    ]
    page.goto(url)
    page.wait_for_load_state("networkidle")

    _open_sessions(page)

    # No phase buttons for a label-less session.
    assert page.locator("[data-sdk-phase]").count() == 0

    page.locator(".sdk-session-item").first.click()
    page.wait_for_timeout(500)

    content = page.locator("#sdk-session-content")
    assert content.is_visible()
    content_text = content.text_content() or ""
    assert "raw blob line one" in content_text
    assert "raw blob line two" in content_text
    # Legacy blob is rendered verbatim (no turn parsing), so no typed blocks.
    assert page.locator(".sdk-turn").count() == 0


def test_turn_content_is_escaped(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    payload = "<script>window.__turnxss=true</script>"
    data["sdk_sessions"] = [
        _stamped(
            "10-00-00_contemplate_x.jsonl",
            "contemplate",
            turns=[_turn("tool_result", "tool", payload)],
        ),
    ]
    page.goto(url)
    page.wait_for_load_state("networkidle")

    _open_sessions(page)
    page.locator(".sdk-session-item").first.click()
    page.wait_for_timeout(500)

    content_html = page.locator("#sdk-session-content").inner_html()
    assert "<script>" not in content_html
    assert "&lt;script&gt;" in content_html

    # The injected script must not have executed or added a live node.
    fired = page.evaluate("window.__turnxss")
    assert fired is None or fired is False
    total_scripts = page.evaluate("document.querySelectorAll('script').length")
    assert total_scripts == 1


def test_unchanged_poll_repaints_neither_pane(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """A tick that found nothing new must not rebuild either pane.

    The list rebuild resets its scroll and the transcript rebuild wipes whatever
    text the operator was selecting, so both panes are gated on a changed
    signature; the transcript's gate also skips the fetch, which reads the whole
    sidecar. Probe attributes set on the live nodes survive exactly as long as
    the nodes are not replaced.
    """
    url, data, _ = dashboard_server
    data["sdk_sessions"] = [
        _stamped("live.jsonl", "contemplate", turns=[_turn("text", "assistant", "FIRST-TURN")]),
    ]
    page.goto(url)
    page.wait_for_load_state("networkidle")
    _open_sessions(page)

    page.evaluate(
        "() => { document.querySelector('.sdk-session-item').dataset.probe = 'list';"
        " document.querySelector('#sdk-session-content .sdk-turn').dataset.probe = 'turn'; }"
    )
    page.evaluate("() => loadSdkSessions()")
    page.wait_for_timeout(300)

    assert page.locator('.sdk-session-item[data-probe="list"]').count() == 1
    assert page.locator('.sdk-turn[data-probe="turn"]').count() == 1

    # …but a transcript that actually grew still repaints both panes.
    data["sdk_sessions"] = [
        _stamped(
            "live.jsonl",
            "contemplate",
            turns=[
                _turn("text", "assistant", "FIRST-TURN"),
                _turn("text", "assistant", "SECOND-TURN"),
            ],
        ),
    ]
    page.evaluate("() => loadSdkSessions()")
    page.wait_for_timeout(300)

    assert page.locator('.sdk-session-item[data-probe="list"]').count() == 0
    assert page.locator(".sdk-turn").count() == 2
    assert "SECOND-TURN" in (page.locator("#sdk-session-content").text_content() or "")


def test_vanished_session_is_surfaced_not_left_stale(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """A selected session that 404s clears the pane instead of looking live.

    Picking a session pauses the tail, so the selection survives later polls. If
    that session ages out of the retention window its detail fetch 404s — the
    operator must be told, not left reading a dead transcript under a status
    claiming it is live.
    """
    url, data, _ = dashboard_server
    data["sdk_sessions"] = [
        _stamped("doomed.jsonl", "contemplate", turns=[_turn("text", "assistant", "SOON-GONE")]),
        _stamped("survivor.jsonl", "evolve", turns=[_turn("text", "assistant", "STILL-HERE")]),
    ]
    page.goto(url)
    page.wait_for_load_state("networkidle")
    _open_sessions(page)

    page.locator(".sdk-session-item").first.click()
    page.wait_for_timeout(300)
    content = page.locator("#sdk-session-content")
    assert "SOON-GONE" in (content.text_content() or "")

    # The selected session ages out; the list (and the detail route) no longer
    # knows it, so the next poll's detail fetch 404s.
    data["sdk_sessions"] = [
        _stamped("survivor.jsonl", "evolve", turns=[_turn("text", "assistant", "STILL-HERE")]),
    ]
    page.evaluate("() => loadSdkSessions()")
    page.wait_for_timeout(300)

    content_text = content.text_content() or ""
    assert "no longer available" in content_text
    assert "SOON-GONE" not in content_text
    status = page.locator("#sdk-session-status").text_content() or ""
    assert "no longer available" in status
    assert "Following" not in status
    # The dropped selection leaves no session marked in the list.
    assert page.locator(".sdk-session-item.selected").count() == 0


def test_transcript_pane_shows_placeholder_with_nothing_selected(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """With no sessions the transcript pane explains itself, not an empty box."""
    url, data, _ = dashboard_server
    data["sdk_sessions"] = []
    page.goto(url)
    page.wait_for_load_state("networkidle")

    content = page.locator("#sdk-session-content")
    # Present before the first fetch (the template ships the placeholder)…
    assert "Select a session" in (content.text_content() or "")

    _open_sessions(page)

    # …and still present after a fetch that selected nothing, with the status
    # saying the tail is on but has nothing to show rather than claiming a live
    # session.
    assert "Select a session" in (content.text_content() or "")
    assert "no sessions yet" in (page.locator("#sdk-session-status").text_content() or "")
