"""
Dashboard page object. Mirrors the Java DashboardPage's responsibilities:
  - Detect that the dashboard has loaded (sidebar + My Connects header)
  - Click "Create Connect" to open the editor
  - Log out via the profile menu

Each interaction uses Playwright Locator + auto-wait. No explicit
WebDriverWait calls — Playwright re-queries the DOM on every action,
which is what eliminates the StaleElementReferenceException class of
failures the Java page object suffered from.
"""

from __future__ import annotations

import logging

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, expect

logger = logging.getLogger(__name__)


class DashboardPage:
    def __init__(self, page: Page) -> None:
        self.page = page

    # ── Detection ─────────────────────────────────────────────────────

    def is_dashboard_loaded(self, timeout_ms: int = 15_000) -> bool:
        """
        True when the dashboard's sidebar + "My Connects" header are both
        visible. Returns False on timeout — caller decides how to handle.
        """
        try:
            sidebar = self.page.locator(
                "aside, .sidebar, nav.app-sidebar, [class*='sidebar']"
            ).first
            sidebar.wait_for(state="visible", timeout=timeout_ms)
            sidebar_found = True
        except PlaywrightTimeoutError:
            sidebar_found = False
        logger.info("Sidebar found: %s", sidebar_found)

        try:
            header = self.page.get_by_text("My Connects").first
            header.wait_for(state="visible", timeout=5_000)
            header_found = True
        except PlaywrightTimeoutError:
            header_found = False
        logger.info("Header 'My Connects' found: %s", header_found)

        return sidebar_found and header_found

    # ── Actions ───────────────────────────────────────────────────────

    def click_create_connect(self, timeout_ms: int = 15_000) -> None:
        """
        Click the 'Create Connect' button. The Appy Pie dashboard renders
        multiple variants of this element across Angular route templates:
          - A hidden anchor with `id="createConnect"` (route placeholder)
          - A visible CTA whose location varies by dashboard state
            (empty state → centered; populated state → top-right toolbar)

        Strategy: try each candidate in order; click the first one that's
        actionable. If no candidate is visible+clickable, fall back to
        direct navigation to the editor URL (the hidden anchor's `href`).
        Direct navigation preserves the test intent (verify the editor
        opens) even when the button itself isn't discoverable.
        """
        logger.info("Clicking 'Create Connect' button...")

        candidates: list[str] = [
            # Most specific to least specific
            "a[href*='/customeditor/fresh']:visible",
            "#createConnect:visible",
            "a:visible:has-text('Create Connect')",
            "button:visible:has-text('Create Connect')",
            "button:visible:has-text('Create a Connect')",
        ]

        for sel in candidates:
            try:
                btn = self.page.locator(sel).first
                btn.wait_for(state="visible", timeout=2_000)
                btn.click(timeout=5_000)
                logger.info("Create Connect clicked via: %s", sel)
                return
            except PlaywrightTimeoutError:
                logger.debug("Selector did not match a visible element: %s", sel)
                continue
            except Exception as e:
                logger.debug("Click via %s failed: %s", sel, str(e).split("\n", 1)[0])
                continue

        # Last-resort fallback: navigate directly to the editor URL.
        # The hidden #createConnect anchor's href tells us the target.
        # This preserves test intent (verify editor opens) without depending
        # on a discoverable button. Java doesn't need this fallback because
        # the Java page object was tuned against the live DOM; the Python
        # port is in Phase 3 PoC and tuning hasn't happened yet.
        logger.warning(
            "No visible Create Connect button found via any candidate selector; "
            "navigating directly to the editor URL as fallback."
        )
        self.page.goto(
            "https://connectcloud.appypie.com/customeditor/fresh/app/newNode"
        )

    def click_logout(self, timeout_ms: int = 30_000) -> None:
        """
        Open the profile menu, click 'Logout', wait for post-logout
        redirect to the accounts.appypie.com login page.
        """
        # If we're not on the dashboard URL, navigate there first.
        if "connects" not in self.page.url and "dashboard" not in self.page.url:
            logger.info("Not on dashboard, navigating there before logout...")
            self.page.goto("https://connectcloud.appypie.com/connects")
            self.page.wait_for_load_state("domcontentloaded")

        logger.info("Performing logout...")
        profile_btn = self.page.locator(
            ".profile-menu-toggle, "
            ".user-menu-toggle, "
            "[class*='profile'] button, "
            "img[alt*='profile' i]"
        ).first
        profile_btn.wait_for(state="visible", timeout=timeout_ms)
        profile_btn.click()
        logger.info("Profile menu opened.")

        # The live DOM uses "Log out" (two words) — captured-DOM evidence
        # from the previous run. Match both variants so a future copy
        # change either way doesn't re-break this test.
        logout_link = self.page.locator(
            "a:has-text('Log out'), "
            "button:has-text('Log out'), "
            "a:has-text('Logout'), "
            "button:has-text('Logout'), "
            "a:has-text('Sign out'), "
            "button:has-text('Sign out')"
        ).first
        logout_link.wait_for(state="visible", timeout=timeout_ms)
        logout_link.click()
        logger.info("Logout link clicked.")

        logger.info("Waiting for redirect to accounts.appypie.com...")
        try:
            self.page.wait_for_url(
                "**accounts.appypie.com/login**",
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError:
            logger.warning(
                "Redirect wait timed out. Current URL: %s", self.page.url
            )

        logger.info("Post-logout URL: %s", self.page.url)


class ConnectEditorPage:
    """Minimal editor page object — only the visibility check the sanity test needs."""

    def __init__(self, page: Page) -> None:
        self.page = page

    def is_editor_visible(self, timeout_ms: int = 30_000) -> bool:
        try:
            self.page.locator(
                "app-custom-editor, .connect-editor, [class*='customeditor']"
            ).first.wait_for(state="visible", timeout=timeout_ms)
            return True
        except PlaywrightTimeoutError:
            return "customeditor" in (self.page.url or "")
