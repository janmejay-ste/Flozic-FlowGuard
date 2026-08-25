"""
tests/mobile/mobile_targets.py

Single place to point the mobile suite at real URLs and, where a check needs
it, real selectors. Kept separate from the test bodies so pointing this at
staging vs prod, or adjusting a selector after a DOM change, never means
touching test logic.

WHY NOTHING HERE IS A HARDCODED AUTH URL
----------------------------------------
The original version of this file hardcoded accounts.appypie.com/login and
/register. By the time this branch was picked up again that was TWO host
migrations stale — the product moved to authv2.flozic.ai, then to
accounts.flozic.ai, and the password step now lives on its own
/login/continue route. A layout audit pointed at a stale host measures a
redirect chain and reports it as the login page.

So the auth pages are reached the way a real user reaches them: navigate to
the app entry point and let the OAuth flow land wherever it currently lands.
That is host-agnostic by construction, so the next migration cannot silently
invalidate these tests. `pages/auth_state.AUTH_HOSTS` stays the single source
of truth for "did we get there", and the assertion below uses it.

The signup page is reached by following the login page's own "Create an
account" link, because its URL carries per-session OAuth parameters
(state, code_challenge) that cannot be constructed ahead of time.
"""

from __future__ import annotations

import os

from pages.auth_helper import LOGIN_URL
from pages.auth_state import is_on_auth_host
from utils.config import MARKETING_BASE

MARKETING_PAGES = {
    "homepage": f"{MARKETING_BASE}/",
    "pricing": f"{MARKETING_BASE}/pricing",
}

# Above-the-fold selectors are the REAL ones from pages/authv2_helper.py, not
# the best-guess CSS this file originally shipped. A submit button that needs
# scrolling past the on-screen keyboard is the bug this check exists to find,
# and a selector that matches nothing reports "no finding" — a false pass.
_LOGIN_SUBMIT = ("button[type='submit']:has-text('Next')", "Login 'Next' button")
_SIGNUP_SUBMIT = (
    "button[type='submit']:has-text('Create'), button[type='submit']:has-text('Sign up'), "
    "button[type='submit']",
    "Signup submit button",
)

# key -> (entry_url, follow_link_selector_or_None, [(selector, label), ...])
AUTH_PAGES = {
    "login": (LOGIN_URL, None, [_LOGIN_SUBMIT]),
    "signup": (LOGIN_URL, "a:has-text('Create an account')", [_SIGNUP_SUBMIT]),
}


# The initial page load and the OAuth redirect chain need separate budgets.
#
# A single 45s covering both was intermittently too short: 2 of 12 auth tests
# failed on a full run, then both passed in 9.9s when re-run alone. Each test
# opens a fresh context, so each one performs a COMPLETE handshake against
# Cognito — 12 of them back to back, on throttled mobile user agents. Whether
# the slow case is local contention or Cognito rate-limiting repeated
# handshakes, the redirect is the slow half and deserves its own budget.
#
# Override for a slow link or a rate-limited tenant:
#   FLOZIC_MOBILE_REDIRECT_TIMEOUT_MS=120000
_NAV_TIMEOUT_MS = 45_000
try:
    _REDIRECT_TIMEOUT_MS = int(
        os.environ.get("FLOZIC_MOBILE_REDIRECT_TIMEOUT_MS", "") or 90_000
    )
except ValueError:
    _REDIRECT_TIMEOUT_MS = 90_000


def open_auth_page(page, entry_url: str, follow_link: str | None,
                   timeout_ms: int = _NAV_TIMEOUT_MS,
                   redirect_timeout_ms: int = _REDIRECT_TIMEOUT_MS) -> str:
    """Navigate to a live auth page and return its final URL.

    Raises if we never reach a recognised auth host — auditing whatever else
    loaded would produce findings attributed to "login" that describe a
    different page entirely.
    """
    # "domcontentloaded" rather than "networkidle": auth pages run background
    # polling (chat widgets, analytics, a Turnstile challenge) that never goes
    # fully quiet, which made "networkidle" time out on every device.
    page.goto(entry_url, wait_until="domcontentloaded", timeout=timeout_ms)
    if not is_on_auth_host(page.url):
        # Only wait on a navigation if we are not already there — wait_for_url
        # otherwise blocks for a navigation event that will never come.
        page.wait_for_url(
            lambda u: is_on_auth_host(u), timeout=redirect_timeout_ms,
        )

    if follow_link:
        link = page.locator(follow_link).first
        link.wait_for(state="visible", timeout=timeout_ms)
        link.click()
        # The switch is client-side within the Hosted UI, so wait on the path
        # rather than a navigation event.
        page.wait_for_url(
            lambda u: "/signup" in (u or "") or "/register" in (u or ""),
            timeout=redirect_timeout_ms,
        )
        page.wait_for_load_state("domcontentloaded")

    final = page.url
    if not is_on_auth_host(final):
        raise AssertionError(
            f"Never reached a known auth host from {entry_url}. "
            f"Final URL: {final}"
        )
    return final
