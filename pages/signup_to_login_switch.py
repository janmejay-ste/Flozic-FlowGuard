"""
Detect when the flozic.ai build button redirects to the signup page instead
of the login page, log it as an ISSUE, and click the 'Sign in' link to
switch to the login flow.

Observed in: GoHighLevel (slug 'gohighlevel'). The flozic.ai build button on
that app's integrations page routes to:
    https://accounts.appypie.com/register?AIFormAutomationPrompt=...&frompage=...
instead of the usual:
    https://accounts.appypie.com/login?frompage=...

This is a product-side inconsistency worth surfacing as an issue. We log it
at WARNING level (visible in test output + dashboard) so QA can ping the
relevant team to align the routing.
"""

from __future__ import annotations

import logging

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger(__name__)

SIGNUP_PATH_FRAGMENT = "/register"


def is_on_signup(page: Page) -> bool:
    """True when the current URL is on the accounts.appypie.com signup page."""
    url = (page.url or "").lower()
    return SIGNUP_PATH_FRAGMENT in url and "accounts.appypie" in url


def switch_signup_to_login_if_needed(
    page: Page,
    app_key: str | None = None,
    timeout_ms: int = 10_000,
) -> bool:
    """
    If the page is currently on the signup (/register) URL, log a WARNING
    flagging the wrong-redirect issue and click the 'Sign in' link to
    switch to the login form.

    Returns True if a switch was performed, False otherwise.
    """
    if not is_on_signup(page):
        return False

    # ── ISSUE: surface the wrong-redirect with the offending URL so it
    # appears in test logs and the dashboard's failure artifacts.
    logger.warning(
        "[ISSUE] flozic build button for app=%s redirected to SIGNUP page "
        "instead of LOGIN. URL: %s  -- This is a product-side routing "
        "inconsistency; expected /login, got /register.",
        app_key or "<unknown>",
        page.url,
    )

    # Try a cascade of selectors for the 'switch to login' link. Different
    # Appy Pie auth-UI builds use different text/markup for this link.
    link_selectors = [
        "a:has-text('Sign in')",
        "a:has-text('Sign In')",
        "a:has-text('Log in')",
        "a:has-text('Log In')",
        "a:has-text('Login')",
        "a:has-text('Already have an account')",
        "a[href*='accounts.appypie.com/login']",
        "a[href*='/login']",
    ]
    for sel in link_selectors:
        try:
            link = page.locator(sel).first
            link.wait_for(state="visible", timeout=1_500)
            link.click(timeout=5_000)
            logger.info("Clicked signup->login switch link via selector: %s", sel)
            # Wait for the URL to actually flip to /login.
            try:
                page.wait_for_url("**accounts.appypie.com/login**",
                                  timeout=timeout_ms)
            except PlaywrightTimeoutError:
                logger.warning(
                    "Clicked switch link but URL did not change to /login "
                    "within %dms. Current URL: %s",
                    timeout_ms, page.url,
                )
            return True
        except (PlaywrightTimeoutError, Exception):
            continue

    logger.error(
        "[ISSUE] Found signup page but NO 'Sign in' link located. Cannot "
        "switch to login automatically. Page URL: %s",
        page.url,
    )
    return False
