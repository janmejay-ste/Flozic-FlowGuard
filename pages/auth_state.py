"""
Auth-state predicates. Python port of Java `pages.auth.AuthState`.

The Java tests treat any of these conditions as "the user has reached
a valid auth state":
  - The IdP host (accounts.appypie.com) is visible
  - The URL is on an /login or /signup route
  - Local storage / cookies show session tokens

We mirror exactly. Predicates intentionally tolerant — auth flows
have many valid intermediate states.
"""

from __future__ import annotations

from playwright.sync_api import Page


def is_on_idp(page: Page) -> bool:
    """True iff current URL is on the appypie auth IdP host."""
    return "accounts.appypie.com" in (page.url or "")


def is_on_auth_route(page: Page) -> bool:
    """True iff URL contains /login or /signup somewhere."""
    url = (page.url or "").lower()
    return "/login" in url or "/signup" in url


def has_session(page: Page) -> bool:
    """
    True iff localStorage or cookies suggest an active session.
    Matches the Java JS expression in AuthState.hasSession().
    """
    try:
        result = page.evaluate(
            """
            () => {
                const ls = window.localStorage;
                const fromLs = ls && (
                    ls.getItem('authToken') ||
                    ls.getItem('accessToken') ||
                    ls.getItem('token') ||
                    ls.getItem('user_data') ||
                    ls.getItem('userInfo')
                );
                const c = document.cookie || '';
                const fromCookie =
                    c.indexOf('PHPSESSID') !== -1 ||
                    c.indexOf('session') !== -1 ||
                    c.indexOf('auth') !== -1;
                return Boolean(fromLs || fromCookie);
            }
            """
        )
        return bool(result)
    except Exception:
        return False


def is_in_valid_state(page: Page) -> bool:
    """OR of the three predicates — the Java valid-auth-state check."""
    return is_on_idp(page) or is_on_auth_route(page) or has_session(page)


def wait_for_auth_transition(page: Page, timeout_ms: int = 30_000) -> None:
    """
    Wait up to timeout for the page to transition to a recognised auth
    state. Mirrors Java WaitUtils.waitForAuthTransition exactly.
    Raises if no state is reached within timeout.
    """
    import time
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        if is_in_valid_state(page):
            return
        page.wait_for_timeout(500)
    raise RuntimeError(
        f"Auth transition did not occur within {timeout_ms} ms. "
        f"Final URL: {page.url}"
    )
