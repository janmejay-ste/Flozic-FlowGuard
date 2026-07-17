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

# Entry-point URL for unauthenticated navigation. The server redirects to
# authv2.flozic.ai/login (Cognito) when the session is missing — we never
# land on accounts.appypie.com/login directly.
LOGIN_URL = "https://loop.flozic.ai/connects"

def _post_login_url_matches(url: str) -> bool:
    if not url:
        return False
    # Must be the destination, not the login page (which mentions the host
    # in its frompage= query param). Match on path prefix /connects.
    return (
        ("connectcloud.appypie.com/connects" in url)
        or ("loop.flozic.ai/connects" in url)
    )


POST_LOGIN_URL_PATTERN = _post_login_url_matches


def _current_test_name() -> str:
    """Best-effort current pytest test name for telemetry. '' if unknown."""
    return os.environ.get("PYTEST_CURRENT_TEST", "").split(" ")[0] or "<unknown>"


def perform_login(
    page: Page,
    email: str = DEFAULT_EMAIL,
    password: str = DEFAULT_PASSWORD,
    auto_timeout_ms: int = 30_000,
    manual_fallback_minutes: int = 3,
    skip_initial_navigation: bool = False,
    post_login_url_pattern=POST_LOGIN_URL_PATTERN,
    enable_authv2: bool = True,
) -> None:
    """
    Navigate to login, attempt automated login, and wait for post-login redirect.
    If automated login fails (selector drift, captcha, etc.), wait up to
    `manual_fallback_minutes` for the user to complete login manually.

    Two-stage login (current, post late-2026):
        1. accounts.appypie.com/login  (legacy form)
        2. authv2.flozic.ai/login      (Cognito Hosted UI second stage)

    Stage 2 is handled automatically when `enable_authv2=True` (the default).
    The legacy /connects redirect frequently flashes the destination URL
    briefly before bouncing to authv2 — so `wait_for_url(post_login_pattern)`
    can return early on the brief flash, leaving us stranded on the Cognito
    login page. After stage 1 returns, we ALWAYS check whether we're on
    authv2 and complete that form too, then re-wait for the final destination.

    Set `enable_authv2=False` only if you know the flow you're testing
    doesn't go through Cognito (rare).
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

    # ── Login-route detection ─────────────────────────────────────────────────
    # If the current URL is on accounts.appypie.com/login, run the legacy
    # stage-1 form AND record this on the dashboard. Otherwise we assume the
    # OAuth flow has already skipped the legacy form (direct authv2 entry)
    # and we proceed straight to stage-2.
    current_url = page.url or ""
    on_legacy_login = (
        "accounts.appypie.com" in current_url and "/login" in current_url
    )

    try:
        from utils.health_tracker import record_login_route
        route = "legacy-appypie" if on_legacy_login else "flozic-authv2"
        record_login_route(_current_test_name(), route, current_url)
        if on_legacy_login:
            logger.warning(
                "[login-route] LEGACY accounts.appypie.com login detected — "
                "running stage-1 form. URL: %s", current_url,
            )
        else:
            logger.info(
                "[login-route] Not on accounts.appypie.com — skipping stage-1, "
                "proceeding to flozic (authv2) login. URL: %s", current_url,
            )
    except Exception as e:
        logger.debug("Could not record login route: %s", e)

    if on_legacy_login:
        try:
            _automated_login(page, email, password, auto_timeout_ms,
                             post_login_url_pattern=post_login_url_pattern)
            logger.info("Stage-1 login succeeded. URL: %s", page.url)
        except Exception as e:
            first_line = str(e).split("\n", 1)[0]
            logger.warning(
                "Stage-1 automated login failed (%s). Waiting up to %d minutes "
                "for manual login.",
                first_line,
                manual_fallback_minutes,
            )
            page.wait_for_url(
                post_login_url_pattern,
                timeout=manual_fallback_minutes * 60_000,
            )
            logger.info("Stage-1 manual login completed. URL: %s", page.url)
    else:
        logger.info("Stage-1 skipped — not on accounts.appypie.com login.")

    # ── Stage 2: authv2.flozic.ai Cognito Hosted UI ──────────────────────────
    if enable_authv2:
        # Local import to avoid a circular dependency at module load time
        # (authv2_helper imports DEFAULT_EMAIL / DEFAULT_PASSWORD from here).
        from pages.authv2_helper import (
            handle_authv2_login_if_present, is_on_authv2,
        )
        authv2_was_handled = handle_authv2_login_if_present(
            page, email=email, password=password,
        )
        if authv2_was_handled:
            # After Cognito returns through /auth/cognito/callback, the page
            # lands on the original destination (e.g. /connects). Wait for
            # it so the caller doesn't race against an in-flight redirect.
            try:
                page.wait_for_url(
                    post_login_url_pattern,
                    timeout=auto_timeout_ms,
                )
                logger.info("Stage-2 (authv2) login completed. Final URL: %s",
                            page.url)
            except Exception as e:
                logger.warning(
                    "Stage-2 succeeded but final destination URL never matched "
                    "the pattern (%s). Current URL: %s",
                    str(e).split("\n", 1)[0], page.url,
                )
        elif is_on_authv2(page):
            logger.warning(
                "Page is on authv2 but handler reported it didn't run. "
                "Caller may need to handle this manually. URL: %s", page.url,
            )


def _automated_login(
    page: Page,
    email: str,
    password: str,
    timeout_ms: int,
    post_login_url_pattern=POST_LOGIN_URL_PATTERN,
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
