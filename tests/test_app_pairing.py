"""
Phase 1 — Diagnostic migration of testing.AppPairingTest.

Java source: src/test/java/testing/AppPairingTest.java (1000+ lines)

This is the Phase 1 diagnostic target chosen because Java currently
FAILS this test consistently (per recent e2e run log: '[Net Suite]
Second app card not found'). The hypothesis being tested:

| Outcome (Python) | Interpretation |
|---|---|
| All 6 apps pass | Java failure was framework-related; Playwright handles it |
| Same app(s) fail | Product/selector drift; not stack-specific |
| Different apps fail | New investigation required |
| All apps fail | Suggests Python port is wrong, or product/page is broken |

Scope intentionally narrowed vs Java for Phase 1 purposes:
  - Tests the app-pairing surface up to (and excluding) the Automate
    button click that triggers auth redirect
  - Auth flow + editor validation is R-1-blocked anyway; skipping it
    here means each iteration runs headless-clean
  - Per-app pass/fail via pytest.mark.parametrize gives clean
    diagnostic output (vs Java's all-in-one method)

Cohort: baseline-smoke (no login required for the pairing surface
up to the Automate click).
"""

from __future__ import annotations

import logging

import pytest
from playwright.sync_api import Page

from pages.auth_helper import perform_login
from pages.dashboard_page import DashboardPage
from pages.marketing.app_directory_page import AppDirectoryPage
from pages.signup_to_login_switch import switch_signup_to_login_if_needed
from utils.js_console_monitor import JsConsoleMonitor
from utils.test_category import test_category

logger = logging.getLogger(__name__)

# Mirror Java FIRST_APPS list exactly. If Java fails on Net Suite
# (current state), we want to test the same app set so the
# comparison is direct.
APPS_TO_TEST = [
    "Airbnb",
    "Net Suite",
    "Slack",
    "Trello",
    "Google Sheets",
    "Shopify",
]


@test_category(
    type="REGRESSION",
    requires_login=True,
    feature="App Pairing",
)
class TestAppPairing:
    """
    Phase 1 diagnostic — verifies the app-pairing surface flow up to
    the Automate-button-click point. Each app is a separate parametrized
    test so the pytest report shows precisely which apps work and
    which don't, instead of stopping at the first failure.
    """

    @pytest.fixture(autouse=True)
    def setup(
        self,
        page: Page,
        console_monitor: JsConsoleMonitor,
    ) -> AppDirectoryPage:
        self._page = page
        self._console = console_monitor
        self._app_dir = AppDirectoryPage(page)
        return self._app_dir

    @pytest.mark.parametrize("app_name,iteration", list(enumerate(APPS_TO_TEST)))
    def test_app_pairing_search_flow(self, app_name: int, iteration: str) -> None:
        # Argument order in parametrize: (idx, name) — unpack semantically
        idx, name = app_name, iteration
        logger.info("── Iteration %d/%d: %s ──", idx + 1, len(APPS_TO_TEST), name)

        # Step 1: navigate to App Directory
        self._app_dir.navigate()
        logger.info("[%s] Step 1: Navigated to App Directory", name)

        # Step 2: search for the app
        self._app_dir.search_for_app(name)
        logger.info("[%s] Step 2: Search stabilized", name)

        # Step 3: find and click the first app card
        first_found = self._app_dir.find_and_click_app_card(name)
        assert first_found, (
            f"[{name}] Gate 1 — first app card not found after virtual scroll. "
            f"Current URL: {self._page.url}. See SEARCH DIAGNOSTIC warning in "
            f"logs for DOM state."
        )
        logger.info("[%s] Step 3: Clicked verified app card", name)

        # Allow the click to settle (URL transition / panel expansion)
        self._page.wait_for_load_state("domcontentloaded", timeout=15_000)
        self._page.wait_for_timeout(500)

        # Step 4: click the '+' icon to open the add-app panel
        # Non-fatal: some dashboard states already show the panel.
        self._app_dir.click_plus_icon()
        logger.info("[%s] Step 4: Plus-icon click attempted", name)

        # Step 5: click a second app card by iteration index
        second_found = self._app_dir.click_second_app_card(idx)
        assert second_found, (
            f"[{name}] Gate 2 — second app card not found. This is the gate "
            f"the Java AppPairingTest currently fails at for some apps. "
            f"Current URL: {self._page.url}."
        )
        logger.info("[%s] Step 5: Clicked second app card (idx=%d)", name, idx)

        # Step 6: allow the URL to settle before continuing.
        self._page.wait_for_load_state("domcontentloaded", timeout=15_000)
        self._page.wait_for_timeout(500)
        logger.info(
            "[%s] Step 6 DONE: Pairing surface verified. URL: %s",
            name,
            self._page.url,
        )

        # ── Step 7: click the Automate button to trigger the auth redirect ──
        clicked = self._app_dir.click_automate_button()
        if not clicked:
            logger.warning(
                "[%s] Step 7: Automate button not found — skipping post-pairing "
                "sign-in verification.", name,
            )
            # Still gate on console errors before returning.
            fatal = self._console.fatal_count()
            assert fatal == 0, (
                f"[{name}] {fatal} FAIL-severity JS console errors: "
                f"{self._console.errors}"
            )
            return
        logger.info("[%s] Step 7: Clicked Automate — auth redirect expected.", name)

        # ── Step 8: if we landed on the signup page, switch to login ─────────
        # Some apps misroute the Automate button to /register; the helper
        # detects that and clicks the 'Sign in' link to flip to the login form.
        switch_signup_to_login_if_needed(self._page, app_key=name)

        # ── Step 9: complete login (handles both legacy accounts.appypie.com
        # and direct flozic authv2 paths). For app-pairing, the post-login
        # destination is /customeditor (auto-built workflow), NOT /connects
        # like the default predicate expects — so we widen the pattern here
        # to avoid a 30s wasted timeout. Scoped to this test only. ───────────
        def _app_pair_post_login(url: str) -> bool:
            if not url:
                return False
            return (
                "connectcloud.appypie.com/connects" in url
                or "loop.flozic.ai/connects" in url
                or "connectcloud.appypie.com/customeditor" in url
                or "loop.flozic.ai/customeditor" in url
            )

        perform_login(
            self._page,
            auto_timeout_ms=30_000,
            manual_fallback_minutes=3,
            skip_initial_navigation=True,
            post_login_url_pattern=_app_pair_post_login,
        )
        logger.info("[%s] Step 9: Login complete. URL: %s", name, self._page.url)

        # ── Step 10: host observation (NOT strict) ───────────────────────────
        # Partial-migration window: both connectcloud.appypie.com and
        # loop.flozic.ai are valid post-login destinations. We log which
        # host landed so it shows up on the dashboard, but do NOT fail the
        # test on host choice — that gate returns once the backend fully
        # cuts over to loop.flozic.ai.
        final_url = self._page.url or ""
        if "loop.flozic.ai" in final_url:
            logger.info("[%s] Step 10: post-login on loop.flozic.ai. URL: %s",
                        name, final_url)
        elif "connectcloud.appypie.com" in final_url:
            logger.warning(
                "[%s] Step 10: post-login still on legacy "
                "connectcloud.appypie.com (acceptable during migration). URL: %s",
                name, final_url,
            )
        else:
            logger.warning("[%s] Step 10: post-login on unexpected host. URL: %s",
                           name, final_url)

        # ── Step 11: confirm an app-pair workflow was created ────────────────
        # The post-login destination is either /customeditor (a workflow
        # auto-built from the pairing) or /connects (the dashboard list,
        # from which we can infer the pair landed). Either is acceptable;
        # we only fail if NEITHER is true.
        try:
            self._page.wait_for_url(
                lambda u: ("/customeditor" in (u or ""))
                          or ("/connects" in (u or "")),
                timeout=60_000,
            )
            workflow_url = self._page.url or ""
            workflow_kind = (
                "customeditor" if "/customeditor" in workflow_url else "connects"
            )
            logger.info(
                "[%s] Step 11 DONE: App-pair workflow surfaced (%s). URL: %s",
                name, workflow_kind, workflow_url,
            )
        except Exception as e:
            raise AssertionError(
                f"[{name}] Gate 4 — app-pair workflow not created. Neither "
                f"/customeditor nor /connects reached after login. "
                f"Current URL: {self._page.url}. Cause: {str(e).splitlines()[0]}"
            )

        # ── Step 12: logout via profile dropdown, navigate back to App
        # Directory so the next iteration starts clean. Each parametrized
        # case gets a fresh browser context anyway, so logout is best-effort
        # hygiene — not a hard gate.
        try:
            DashboardPage(self._page).click_logout()
            logger.info("[%s] Step 12: Logout complete.", name)
        except Exception as e:
            logger.warning(
                "[%s] Step 12: Logout failed (non-fatal — fresh context next "
                "iteration): %s", name, str(e).splitlines()[0],
            )

        try:
            self._app_dir.navigate()
            logger.info("[%s] Step 12b: Back at App Directory for next pair.", name)
        except Exception as e:
            logger.warning(
                "[%s] Step 12b: Return-to-directory failed (non-fatal): %s",
                name, str(e).splitlines()[0],
            )

        # Console-error check: fatal_count uses the same origin-aware
        # classifier as Java's JsConsoleMonitor. Zero fatal errors
        # means no first-party app-level JS broke during the flow.
        fatal = self._console.fatal_count()
        assert fatal == 0, (
            f"[{name}] {fatal} FAIL-severity JS console errors during pairing flow: "
            f"{self._console.errors}"
        )
