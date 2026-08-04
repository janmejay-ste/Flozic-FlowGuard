"""
Second-stage login on authv2.flozic.ai (AWS Cognito Hosted UI).

After the first-stage login at accounts.appypie.com succeeds, the OAuth
flow may redirect to a Cognito-hosted login page at authv2.flozic.ai
which prompts for username + password again on a two-step form:

    1. Username input  (name="username", placeholder="Enter username")
    2. Click 'Next' button
    3. Password input  (name="password", placeholder="Enter password")
    4. Click 'Continue' button
    5. Cognito redirects to connectcloud.appypie.com/auth/cognito/callback
       which then takes the user to the destination (e.g. /customeditor).

Selectors:
  The AWS UI library (awsui_*) ships hashed CSS class suffixes that change
  between deployments. Use stable attributes: input[name=...] and the
  visible button text instead of awsui_* class names.

This is described by the user as a temporary interface, so we keep it
isolated in its own helper to be easy to remove later.
"""

from __future__ import annotations

import logging

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from pages.auth_helper import DEFAULT_EMAIL, DEFAULT_PASSWORD

logger = logging.getLogger(__name__)

AUTHV2_HOST_FRAGMENT = "authv2.flozic.ai"


def is_on_authv2(page: Page) -> bool:
    """True when the current URL is on the authv2.flozic.ai Cognito host."""
    return AUTHV2_HOST_FRAGMENT in (page.url or "")


def handle_authv2_login_if_present(
    page: Page,
    email: str = DEFAULT_EMAIL,
    password: str = DEFAULT_PASSWORD,
    settle_timeout_ms: int = 8_000,
    step_timeout_ms: int = 20_000,
) -> bool:
    """
    If the page is currently on authv2.flozic.ai (or navigates there shortly),
    complete the two-step login form.

    Returns True if the authv2 login was performed, False if the page was
    not on authv2 within `settle_timeout_ms` (which is normal for accounts
    that don't need the second-stage handshake).

    The caller is responsible for waiting for the final destination URL
    after this returns (e.g. /customeditor).
    """
    # Give the OAuth redirect a moment to land on authv2 if it's going to.
    try:
        page.wait_for_url(f"**{AUTHV2_HOST_FRAGMENT}**", timeout=settle_timeout_ms)
    except PlaywrightTimeoutError:
        if not is_on_authv2(page):
            logger.info("Not on authv2 (URL=%s) — skipping second-stage login.", page.url)
            return False

    # Idempotency guard: if we're on /verifyPassword, credentials were already
    # submitted by a prior call — Cognito is mid-redirect. Don't re-enter the form.
    current_url = page.url or ""
    if "/verifyPassword" in current_url or "/auth/cognito/callback" in current_url:
        logger.info("authv2 already past login form (URL=%s) — skipping.", current_url)
        return False

    # Signup-page guard: some flows (notably the conversational-agent OAuth)
    # land on authv2.flozic.ai/signup instead of /login. Filling the signup
    # form as if it were a login would hang because the field/next-button
    # semantics differ. Detect signup, click the "Sign in" link to flip to
    # /login, then continue the normal credential submit. This handles the
    # product bug where /agent/builder redirects to signup post-OAuth.
    if "/signup" in current_url:
        logger.warning(
            "[ISSUE] authv2 landed on /signup instead of /login (URL=%s). "
            "Attempting signup→login switch before submitting credentials.",
            current_url,
        )
        # Import lazily to avoid a circular dependency at module load time.
        from pages.signup_to_login_switch import switch_signup_to_login_if_needed
        switched = switch_signup_to_login_if_needed(
            page, app_key="authv2", timeout_ms=step_timeout_ms,
        )
        if not switched:
            logger.error(
                "authv2 was on /signup but the switch to /login failed. "
                "Cannot proceed with credential submit. URL: %s", page.url,
            )
            return False

    logger.info("On authv2 Cognito login (%s). Submitting credentials.", page.url)

    # ── Step 1: username ─────────────────────────────────────────────────────
    # Use a short probe first — if the username field isn't there within a few
    # seconds, the form isn't actually present (e.g. redundant call after the
    # form was already submitted). Return False instead of waiting 20s.
    username_field = page.locator("input[name='username']").first
    try:
        username_field.wait_for(state="visible", timeout=3_000)
    except PlaywrightTimeoutError:
        logger.info(
            "authv2 username field not visible within 3s (URL=%s) — "
            "form likely already submitted; skipping.", page.url,
        )
        return False
    username_field.click()
    username_field.fill(email)
    logger.info("[authv2] Username entered.")

    # ── Step 2: Next button ──────────────────────────────────────────────────
    next_btn = page.locator(
        "button[type='submit']:has-text('Next')"
    ).first
    next_btn.wait_for(state="visible", timeout=step_timeout_ms)
    next_btn.click()
    logger.info("[authv2] Next clicked.")

    # ── Step 3: password ─────────────────────────────────────────────────────
    # The password field is rendered after Cognito acknowledges the username,
    # which can take a network round-trip. Allow ample time.
    password_field = page.locator("input[name='password']").first
    password_field.wait_for(state="visible", timeout=step_timeout_ms)
    password_field.click()
    password_field.fill(password)
    logger.info("[authv2] Password entered.")

    # ── Step 4: Continue button ──────────────────────────────────────────────
    continue_btn = page.locator(
        "button[type='submit']:has-text('Continue')"
    ).first
    continue_btn.wait_for(state="visible", timeout=step_timeout_ms)
    continue_btn.click()
    logger.info("[authv2] Continue clicked. Cognito will redirect to the callback URL.")

    return True
