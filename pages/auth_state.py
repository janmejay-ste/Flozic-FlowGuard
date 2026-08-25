"""
Auth-state predicates. Python port of Java `pages.auth.AuthState`.

The Java tests treat any of these conditions as "the user has reached
a valid auth state":
  - A recognised IdP host is visible (see AUTH_HOSTS)
  - The URL is on an /login or /signup route
  - Local storage / cookies show session tokens

We mirror exactly. Predicates intentionally tolerant — auth flows
have many valid intermediate states.

AUTH_HOSTS is the single source of truth for "is this an auth host?" across
the framework. It lives here because both auth_helper and authv2_helper
already sit above this module, so there is no import cycle.
"""

from __future__ import annotations

import os

from playwright.sync_api import Page


# Recognised auth IdP hosts, current first.
#
#   accounts.flozic.ai   — the live AWS Cognito Hosted UI (as of 2026-08-11)
#   authv2.flozic.ai     — the same Hosted UI's previous hostname. Kept so a
#                          product-side rollback doesn't break the suite.
#   accounts.appypie.com — the legacy pre-Cognito login form.
#
# When the auth host moves again, this tuple is the only place that needs
# editing. Point the suite at a staging IdP without a code change via
# FLOZIC_AUTH_HOSTS="host-a,host-b".
_DEFAULT_AUTH_HOSTS = (
    "accounts.flozic.ai",
    "authv2.flozic.ai",
    "accounts.appypie.com",
)

AUTH_HOSTS: tuple[str, ...] = tuple(
    h.strip() for h in os.environ.get("FLOZIC_AUTH_HOSTS", "").split(",") if h.strip()
) or _DEFAULT_AUTH_HOSTS


def is_on_auth_host(url: str | None) -> bool:
    """True iff `url` is on any recognised auth host. Takes a string rather
    than a Page so callers inside `wait_for_url` predicates can use it."""
    u = url or ""
    return any(host in u for host in AUTH_HOSTS)


def is_on_idp(page: Page) -> bool:
    """True iff current URL is on a recognised auth IdP host."""
    return is_on_auth_host(page.url)


def is_on_auth_route(page: Page) -> bool:
    """True iff URL contains a recognised auth path segment."""
    url = (page.url or "").lower()
    return "/login" in url or "/signup" in url or "/register" in url


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
