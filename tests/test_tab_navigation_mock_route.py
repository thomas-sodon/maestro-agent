"""Fast-gate regression test for the tab-navigation mock fixture's route (#456).

This lives outside ``tests/browser/`` on purpose. ``tests/browser/conftest.py``
auto-stamps ``pytest.mark.browser`` on every test under that directory, so the
default ``pytest tests/`` gate (which runs with ``-m 'not browser'``) deselects
anything placed there. Keeping this pure ``TestClient`` check here means it runs
in the fast default gate, guarding the mock/production narrative-route alignment
even when the slow Playwright suite is skipped.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.browser.test_tab_navigation import _create_unmodified_app


def test_unmodified_app_serves_narrative_at_real_path() -> None:
    """``_create_unmodified_app`` serves the narrative at the production route (#456).

    The browser-test fixture must register the narrative endpoint at the same path
    the real dashboard serves it from (``journal_narrative`` at
    ``/api/journal/narrative``) and the conftest sibling uses, not the stale bare
    ``/api/narrative``. ``TestClient`` exercises the route in-process with no live
    server, subprocess, or network.
    """
    client = TestClient(_create_unmodified_app())
    response = client.get("/api/journal/narrative")
    assert response.status_code == 200
    assert response.json() == {"narrative": ""}
    # The stale bare path was replaced, not duplicated.
    assert client.get("/api/narrative").status_code == 404
