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

SIGNUP_PATH_FRAGMENTS = ("/register", "/signup")
SIGNUP_HOST_FRAGMENTS = ("accounts.appypie", "authv2.flozic.ai")


def _has_signup_dom_markers(page: Page) -> bool:
    """
    DOM-based fallback: the authv2.flozic.ai Cognito Hosted UI renders the
    signup form at the root URL (no /signup path), so URL-only detection
    misses it. Identify the signup form by its content — 'Confirm password'
    input, the 'Sign up' submit button, OR the 'Have an account already?'
    link block all uniquely identify the signup variant.
    """
    markers = [
        "input[placeholder='Reenter password']",
        "input[name='confirm_password']",
        "button[type='submit']:has-text('Sign up')",
        "text=Have an account already?",
        "text=Confirm password",
    ]
    for sel in markers:
        try:
            if page.locator(sel).first.is_visible(timeout=500):
                return True
        except Exception:
            continue
    return False


def is_on_signup(page: Page) -> bool:
    """
    True when the page is showing a signup form — either by URL match
    (legacy accounts.appypie.com/register or authv2 /signup) OR by DOM
    fingerprint (authv2 renders signup at the root URL without a /signup
    path, so we fall back to spotting the 'Confirm password' / 'Sign up'
    elements that only appear on the signup variant).
    """
    url = (page.url or "").lower()
    on_signup_path = any(p in url for p in SIGNUP_PATH_FRAGMENTS)
    on_signup_host = any(h in url for h in SIGNUP_HOST_FRAGMENTS)
    if on_signup_path and on_signup_host:
        return True
    # Fallback: authv2 host + signup-specific DOM markers.
    if "authv2.flozic.ai" in url and _has_signup_dom_markers(page):
        return True
    return False


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
    # Appy Pie auth-UI builds (legacy accounts.appypie.com + new authv2 Cognito
    # Hosted UI with awsui_* classes) use different text/markup for this link.
    #
    # IMPORTANT — selector specificity matters here. The flozic signup page
    # contains a "Sign in with Google" button alongside the "Sign in" link.
    # A loose `a:has-text('Sign in')` partial-match selector incorrectly hits
    # the Google button. We therefore prefer:
    #   1. href-based selectors (link points to /login — Google button doesn't)
    #   2. exact-text selectors (':text-is' requires the full text equality)
    # Fallback partial-text selectors come last AND only inside the
    # 'Have an account already?' block, where the Google button can't reach.
    link_selectors = [
        # 1. href-based — most specific, can't false-match the Google button.
        "a[href*='/login?client_id=']",
        "a[href*='accounts.appypie.com/login']",
        "a[href*='/login']:not([href*='google'])",
        # 2. Exact-text — won't match 'Sign in with Google' (different full text).
        "a:text-is('Sign in')",
        "a:text-is('Sign In')",
        "a:text-is('Log in')",
        "a:text-is('Log In')",
        "a:text-is('Login')",
        # 3. authv2 Cognito Hosted UI: anchor with awsui_link_* class + exact text
        "a[class*='awsui_link']:text-is('Sign in')",
        # 4. Scoped by container — only links inside the 'Have an account already?'
        #    block (a <p> containing that exact phrase), so Google can't reach.
        "p:has-text('Have an account already?') a",
    ]
    for sel in link_selectors:
        try:
            link = page.locator(sel).first
            link.wait_for(state="visible", timeout=1_500)
            link.click(timeout=5_000)
            logger.info("Clicked signup->login switch link via selector: %s", sel)
            # Wait for the URL to flip to a /login path on EITHER host.
            try:
                page.wait_for_url(
                    lambda u: ("/login" in (u or ""))
                              and ("accounts.appypie" in u or "authv2.flozic.ai" in u),
                    timeout=timeout_ms,
                )
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
