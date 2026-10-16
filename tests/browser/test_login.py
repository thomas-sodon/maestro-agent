"""Browser tests for the login page and cookie authentication flow."""

from __future__ import annotations

from typing import Any

import pytest

from tests.browser.conftest import AUTH_STUB_PLAN_HASH


@pytest.mark.browser
def test_unauthenticated_redirects_to_login(
    page: Any,
    auth_dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]], str],
) -> None:
    """Visiting /account without a session cookie redirects to /login."""
    url, *_ = auth_dashboard_server
    page.goto(f"{url}/account")
    page.wait_for_url("**/login**")
    assert "/login" in page.url
    assert page.locator("input#token").is_visible()


@pytest.mark.browser
def test_login_with_correct_token(
    page: Any,
    auth_dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]], str],
) -> None:
    """Entering the correct token logs in and shows the dashboard at /account."""
    url, _data, _log, token = auth_dashboard_server
    page.goto(f"{url}/login")
    page.fill("input#token", token)
    page.click("button[type='submit']")
    page.wait_for_url(f"{url}/account")
    # Dashboard loaded — check for a known element
    page.wait_for_selector("h1", timeout=5000)
    assert "Maestro" in page.content()


@pytest.mark.browser
def test_login_with_wrong_token_shows_error(
    page: Any,
    auth_dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]], str],
) -> None:
    """Entering a wrong token shows an error message."""
    url, *_ = auth_dashboard_server
    page.goto(f"{url}/login")
    page.fill("input#token", "wrong-token")
    page.click("button[type='submit']")
    page.wait_for_url("**/login?error=1")
    error_el = page.locator("#error-msg")
    assert error_el.is_visible()
    assert "Invalid token" in error_el.text_content()


@pytest.mark.browser
def test_logout_clears_session(
    page: Any,
    auth_dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]], str],
) -> None:
    """After logging out, accessing /account redirects back to /login."""
    url, _data, _log, token = auth_dashboard_server
    # Log in first
    page.goto(f"{url}/login")
    page.fill("input#token", token)
    page.click("button[type='submit']")
    page.wait_for_url(f"{url}/account")
    # Now log out via POST using a form submission (follows redirect natively)
    page.evaluate(
        """() => {
            const f = document.createElement('form');
            f.method = 'POST';
            f.action = '/logout';
            document.body.appendChild(f);
            f.submit();
        }""",
    )
    page.wait_for_url("**/login**")
    assert "/login" in page.url


# ---------------------------------------------------------------------------
# CSRF double-submit flow (proposal 022)
# ---------------------------------------------------------------------------


def _login(page: Any, url: str, token: str) -> None:
    page.goto(f"{url}/login")
    page.fill("input#token", token)
    page.click("button[type='submit']")
    page.wait_for_url(f"{url}/account")


@pytest.mark.browser
def test_sign_out_control_visible_and_clears_cookies(
    page: Any,
    auth_dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]], str],
) -> None:
    """The header sign-out control appears once authed and logs out on click.

    Unlike ``test_logout_clears_session`` (which POSTs to ``/logout`` from a
    JS-constructed form), this drives the real UI control added in proposal 122:
    ``index.js`` reveals it only when the ``maestro_csrf`` cookie is present, and
    clicking it must land on ``/login`` with both the session and CSRF cookies
    cleared.
    """
    url, _data, _log, token = auth_dashboard_server
    _login(page, url, token)

    # revealSignOut() unhides the control once the maestro_csrf cookie is present.
    sign_out = page.locator(".sign-out-btn")
    sign_out.wait_for(state="visible")
    assert sign_out.is_visible()

    names_before = {c["name"] for c in page.context.cookies()}
    assert "maestro_token" in names_before
    assert "maestro_csrf" in names_before

    sign_out.click()
    page.wait_for_url("**/login**")
    assert "/login" in page.url

    names_after = {c["name"] for c in page.context.cookies()}
    assert "maestro_token" not in names_after
    assert "maestro_csrf" not in names_after


@pytest.mark.browser
def test_login_sets_js_readable_csrf_cookie(
    page: Any,
    auth_dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]], str],
) -> None:
    """Login sets maestro_csrf readable by JS; the session cookie stays HttpOnly."""
    url, _data, _log, token = auth_dashboard_server
    _login(page, url, token)
    cookies = {c["name"]: c for c in page.context.cookies()}
    assert "maestro_csrf" in cookies
    assert cookies["maestro_csrf"]["httpOnly"] is False
    assert cookies["maestro_token"]["httpOnly"] is True
    # JS can actually read it — that's what the fetch helper relies on.
    js_cookie = page.evaluate("document.cookie")
    assert "maestro_csrf=" in js_cookie


@pytest.mark.browser
def test_mutation_without_csrf_header_is_rejected(
    page: Any,
    auth_dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]], str],
) -> None:
    """A cookie-authenticated POST that omits X-CSRF-Token gets a 403."""
    url, _data, _log, token = auth_dashboard_server
    _login(page, url, token)
    status = page.evaluate(
        """() => fetch('/api/plans/any/approve', { method: 'POST' })
            .then(r => r.status)""",
    )
    assert status == 403


@pytest.mark.browser
def test_mutation_with_csrf_header_is_accepted(
    page: Any,
    auth_dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]], str],
) -> None:
    """Echoing the cookie in X-CSRF-Token passes the middleware (200).

    The body carries the stub plan's 021 content hash so the request
    travels the full path: CSRF middleware → body validation → approve.
    """
    url, _data, _log, token = auth_dashboard_server
    _login(page, url, token)
    status = page.evaluate(
        """(contentHash) => {
            const m = document.cookie.match(/(?:^|;\\s*)maestro_csrf=([^;]+)/);
            return fetch('/api/plans/any/approve', {
                method: 'POST',
                headers: {
                    'X-CSRF-Token': m[1],
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ content_hash: contentHash }),
            }).then(r => r.status);
        }""",
        AUTH_STUB_PLAN_HASH,
    )
    assert status == 200
