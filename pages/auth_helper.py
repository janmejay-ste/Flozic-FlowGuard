"""
Authentication helper. Python equivalent of Java's ManualLoginHelper.

The Java helper had a 3-minute fallback to manual login if automated
login failed. We replicate the same fallback here so the test can
recover from auth-form layout changes without re-coding.
"""

from __future__ import annotations

import logging
import os

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, expect

logger = logging.getLogger(__name__)

# Test credentials — read from environment ONLY. Never hardcode secrets in
# source (they would leak into version control). Set these before running:
#   PowerShell:  $env:AUTOMATE_EMAIL="..."; $env:AUTOMATE_PASSWORD="..."
#   bash:        export AUTOMATE_EMAIL=... AUTOMATE_PASSWORD=...
# or place them in a local .env file (gitignored).
DEFAULT_EMAIL = os.environ.get("AUTOMATE_EMAIL", "")
DEFAULT_PASSWORD = os.environ.get("AUTOMATE_PASSWORD", "")

if not DEFAULT_EMAIL or not DEFAULT_PASSWORD:
    logger.warning(
        "AUTOMATE_EMAIL / AUTOMATE_PASSWORD not set in the environment. "
        "Login-required tests will fall back to the manual-login window. "
        "Set both env vars (or a gitignored .env) to enable automated login."
    )

# Standard login URL that redirects to the Connect dashboard after auth.
LOGIN_URL = (
    "https://accounts.appypie.com/login"
    "?frompage=https:%2F%2Fconnectcloud.appypie.com%2Fconnects"
    "&website=https:%2F%2Fconnectcloud.appypie.com"
)

# The login URL contains "connectcloud.appypie.com" in its `frompage`
# query string, so a bare `**connectcloud.appypie.com**` glob falsely
# matches the login page itself. Require the path component too.
POST_LOGIN_URL_PATTERN = "**connectcloud.appypie.com/connects**"


def perform_login(
    page: Page,
    email: str = DEFAULT_EMAIL,
    password: str = DEFAULT_PASSWORD,
    auto_timeout_ms: int = 30_000,
    manual_fallback_minutes: int = 3,
    skip_initial_navigation: bool = False,
    post_login_url_pattern: str = POST_LOGIN_URL_PATTERN,
) -> None:
    """
    Navigate to login, attempt automated login, and wait for post-login redirect.
    If automated login fails (selector drift, captcha, etc.), wait up to
    `manual_fallback_minutes` for the user to complete login manually.

    This mirrors the Java ManualLoginHelper behaviour. The fallback is
    intentional — auth UI changes on a different schedule than the rest
    of the product, and forcing the test to fail on every auth-UI tweak
    is more noise than signal.
    """
    if skip_initial_navigation:
        # The caller (e.g. flozic.ai build button) already navigated us to a
        # login URL that carries side-channel params (AIFormAutomate=...) which
        # control where the server redirects us after a successful login.
        # Re-issuing page.goto(LOGIN_URL) would strip those params.
        logger.info("Skipping initial login-page navigation. Current URL: %s", page.url)
    else:
        logger.info("Navigating to login URL: %s", LOGIN_URL)
        page.goto(LOGIN_URL)

    try:
        _automated_login(page, email, password, auto_timeout_ms,
                         post_login_url_pattern=post_login_url_pattern)
        logger.info("Automated login succeeded. URL: %s", page.url)
    except Exception as e:
        first_line = str(e).split("\n", 1)[0]
        logger.warning(
            "Automated login failed (%s). Waiting up to %d minutes for manual login.",
            first_line,
            manual_fallback_minutes,
        )
        page.wait_for_url(
            post_login_url_pattern,
            timeout=manual_fallback_minutes * 60_000,
        )
        logger.info("Manual login completed. URL: %s", page.url)


def _automated_login(
    page: Page,
    email: str,
    password: str,
    timeout_ms: int,
    post_login_url_pattern: str = POST_LOGIN_URL_PATTERN,
) -> None:
    """Inner automated login. Throws on any failure; caller handles fallback."""
    # Email field — multiple selectors to handle minor markup drift.
    email_field = page.locator(
        "#testing, input.emailInput, input[type='email'], input[name='email']"
    ).first
    email_field.wait_for(state="visible", timeout=timeout_ms)
    email_field.click()  # focus required — password field is only rendered
                          # after email field is interacted with on this UI
    email_field.fill(email)
    logger.info("Email entered.")

    # Blur the email field so the form's onBlur validator reveals the
    # password field. Pressing Tab triggers blur AND advances focus to
    # the next focusable element (often the password input once it mounts).
    email_field.press("Tab")
    page.wait_for_timeout(500)

    password_field = page.locator(
        "#password, input[type='password'], input[name='password']"
    ).first
    # Allow extra time — password input is created reactively after blur,
    # not present in the initial DOM.
    password_field.wait_for(state="visible", timeout=timeout_ms)
    password_field.click()
    password_field.fill(password)
    logger.info("Password entered.")

    page.wait_for_timeout(500)

    login_btn = page.locator(
        "button.login-btns, button[type='submit'], input[type='submit']"
    ).first
    login_btn.wait_for(state="visible", timeout=timeout_ms)
    login_btn.click()
    logger.info("Login button clicked. Waiting for redirect.")

    # Wait for the post-login URL pattern. If we land somewhere else,
    # treat that as a failure and let the caller decide on fallback.
    page.wait_for_url(post_login_url_pattern, timeout=timeout_ms)
