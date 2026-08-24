"""
Second-stage login on the AWS Cognito Hosted UI.

The host is not hardcoded — see COGNITO_HOSTS below. It was authv2.flozic.ai
and is accounts.flozic.ai as of 2026-08.

After the first-stage login at accounts.appypie.com succeeds, the OAuth
flow may redirect to a Cognito-hosted login page (see COGNITO_HOSTS)
which prompts for username + password again on a two-step form:

    1. Username input  (name="username", placeholder="Enter username")
       URL: <cognito-host>/login?client_id=...
    2. Click 'Next' button
    3. Password input  (name="password", placeholder="Enter password")
       URL: <cognito-host>/login/continue?client_id=...     (as of 2026-08-18)
    4. Click 'Continue' button
    5. Cognito redirects to connectcloud.appypie.com/auth/cognito/callback
       which then takes the user to the destination (e.g. /customeditor).

The step-3 URL is its own route, and it carries the whole authorize parameter
set forward (client_id, response_type, scope, redirect_uri, state,
code_challenge). That matters for re-entrancy — see PASSWORD_STEP_PATHS.

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

# Hosts that serve the Cognito Hosted UI, current first. `accounts.flozic.ai`
# replaced `authv2.flozic.ai` around 2026-08; the old name is retained so a
# rollback doesn't break the suite. The legacy accounts.appypie.com form is
# NOT here — that is stage-1's job (see auth_helper).
COGNITO_HOSTS = ("accounts.flozic.ai", "authv2.flozic.ai")

# Back-compat: some callers imported this single-host constant directly.
AUTHV2_HOST_FRAGMENT = COGNITO_HOSTS[0]

# Routes where the username has been ACCEPTED and the password field is what
# the page is waiting for. Landing here means step 1-2 are already done, so the
# helper must resume at step 3 rather than hunt for a username box that is no
# longer rendered.
#
# Do NOT fold these into POST_SUBMIT_PATHS. The two look similar and mean
# opposite things: this list is "the form wants a password", that one is "the
# form is finished". Treating /login/continue as finished makes the helper
# return before submitting anything, and the login never completes.
PASSWORD_STEP_PATHS = ("/login/continue",)

# Routes reached AFTER the password was submitted. Nothing left to fill; a
# second call here must not re-enter the form.
POST_SUBMIT_PATHS = ("/verifyPassword", "/auth/cognito/callback")


def is_on_password_step(url: str | None) -> bool:
    """True when the URL is the step-3 password route."""
    u = url or ""
    return any(p in u for p in PASSWORD_STEP_PATHS)


def is_on_authv2(page: Page) -> bool:
    """True when the current URL is on a Cognito Hosted UI host."""
    url = page.url or ""
    return any(host in url for host in COGNITO_HOSTS)


def handle_authv2_login_if_present(
    page: Page,
    email: str = DEFAULT_EMAIL,
    password: str = DEFAULT_PASSWORD,
    settle_timeout_ms: int = 8_000,
    step_timeout_ms: int = 20_000,
) -> bool:
    """
    If the page is currently on a Cognito host (or navigates there shortly),
    complete the two-step login form.

    Returns True if the authv2 login was performed, False if the page was
    not on authv2 within `settle_timeout_ms` (which is normal for accounts
    that don't need the second-stage handshake).

    The caller is responsible for waiting for the final destination URL
    after this returns (e.g. /customeditor).
    """
    # Blank-credential guard. DEFAULT_EMAIL/DEFAULT_PASSWORD are
    # os.environ.get(..., "") — unset means empty string, not an exception.
    # Without this check the helper calls fill("") (which CLEARS the field),
    # clicks Next, and Cognito renders "Missing username." That failure looks
    # like a product validation bug in the screenshot and gets triaged as
    # PRODUCT_BUG, sending someone after a defect that does not exist. The
    # real cause is an unset env var, so say so.
    missing = [
        name for name, val in (("AUTOMATE_EMAIL", email), ("AUTOMATE_PASSWORD", password))
        if not (val or "").strip()
    ]
    if missing:
        logger.error(
            "[authv2] Refusing to submit the login form: %s not set. Submitting "
            "empty credentials would produce a 'Missing username.' error that "
            "reads as a product bug. Set them in the environment or a gitignored "
            ".env and re-run.", " and ".join(missing),
        )
        return False

    # Give the OAuth redirect a moment to land on a Cognito host if it's going
    # to. A predicate rather than a glob because there is more than one valid
    # host and wait_for_url takes a single pattern.
    try:
        page.wait_for_url(
            lambda u: any(host in (u or "") for host in COGNITO_HOSTS),
            timeout=settle_timeout_ms,
        )
    except PlaywrightTimeoutError:
        if not is_on_authv2(page):
            logger.info(
                "Not on a Cognito host (URL=%s) — skipping second-stage login. "
                "Known hosts: %s", page.url, ", ".join(COGNITO_HOSTS),
            )
            return False

    # Idempotency guard: the password was already submitted by a prior call and
    # Cognito is mid-redirect. Nothing left to fill.
    current_url = page.url or ""
    if any(p in current_url for p in POST_SUBMIT_PATHS):
        logger.info("authv2 already past login form (URL=%s) — skipping.", current_url)
        return False

    # Re-entrancy: parked on the step-3 route means the username was accepted
    # but the password was not submitted. Resume at step 3 — falling through
    # would probe for a username box that Cognito no longer renders, burn the
    # 3s timeout, and return False as though no login were needed.
    if is_on_password_step(current_url):
        logger.info(
            "[authv2] Already on the password step (URL=%s) — resuming at step 3.",
            current_url,
        )
        return _submit_password(page, password, step_timeout_ms)

    # Signup-page guard: some flows land on <cognito-host>/signup instead of
    # /login. Filling the signup form as if it were a login would hang because
    # the field/next-button semantics differ. Detect signup, click the "Sign in"
    # link to flip to /login, then continue the normal credential submit.
    #
    # Still load-bearing after the conversational-agent tests were removed
    # (2026-08-11) — the GoHighLevel entry point hits the same /signup misroute,
    # and the recovery is what lets that test pass.
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
    # Verify the value landed before clicking Next. `fill()` succeeding only
    # means Playwright found AN element -- if the locator resolved to a hidden
    # duplicate, the visible box stays empty and the click produces a
    # product-looking validation error instead of a locator failure.
    actual = username_field.input_value()
    if actual != email:
        raise AssertionError(
            f"[authv2] Username did not land in the field: expected "
            f"{len(email)} chars, field holds {len(actual)}. The locator "
            f"input[name='username'] likely resolved to the wrong element "
            f"(URL={page.url}). This is locator drift, not a product bug."
        )
    logger.info("[authv2] Username entered and verified.")

    # ── Step 2: Next button ──────────────────────────────────────────────────
    next_btn = page.locator(
        "button[type='submit']:has-text('Next')"
    ).first
    next_btn.wait_for(state="visible", timeout=step_timeout_ms)
    next_btn.click()
    logger.info("[authv2] Next clicked.")

    return _submit_password(page, password, step_timeout_ms)


def _submit_password(page: Page, password: str, step_timeout_ms: int) -> bool:
    """Steps 3-4: fill the password and click Continue.

    Split out because there are two ways to arrive at this point — after
    clicking Next in the same call, or by finding the page already parked on
    PASSWORD_STEP_PATHS from an earlier call. Both must submit identically.
    """
    # ── Step 3: password ─────────────────────────────────────────────────────
    # The password field is rendered after Cognito acknowledges the username,
    # which can take a network round-trip. Allow ample time.
    password_field = page.locator("input[name='password']").first
    password_field.wait_for(state="visible", timeout=step_timeout_ms)
    password_field.click()
    password_field.fill(password)
    # Length only -- never log or assert on the value itself.
    filled = len(password_field.input_value())
    if filled != len(password):
        raise AssertionError(
            f"[authv2] Password did not land in the field: expected "
            f"{len(password)} chars, field holds {filled}. Locator drift on "
            f"input[name='password'] (URL={page.url}), not a product bug."
        )
    logger.info("[authv2] Password entered and verified (%d chars).", filled)

    # ── Step 4: Continue button ──────────────────────────────────────────────
    continue_btn = page.locator(
        "button[type='submit']:has-text('Continue')"
    ).first
    continue_btn.wait_for(state="visible", timeout=step_timeout_ms)
    continue_btn.click()
    logger.info("[authv2] Continue clicked. Cognito will redirect to the callback URL.")

    return True
