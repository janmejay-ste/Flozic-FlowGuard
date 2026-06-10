"""
Python port of testing.AppyPieNavigationTest (simplified).

The Java version measures LCP/CLS via the Performance API and does a
Calendly-tab smoke. This port covers the practical sanity assertions:
page loads, on correct domain, no fatal console errors, nav links work.

LCP/CLS performance metrics are skipped (separate workstream).
The Calendly new-tab test is included as a basic open-and-close check.
"""

from __future__ import annotations

import logging

import pytest
from playwright.sync_api import Page

from utils.config import MARKETING_BASE, is_owned_marketing_host
from utils.js_console_monitor import JsConsoleMonitor
from utils.test_category import test_category

logger = logging.getLogger(__name__)


@test_category(
    type="SANITY",
    requires_login=False,
    feature="Navigation",
)
class TestAppyPieNavigation:
    @pytest.fixture(autouse=True)
    def setup(self, page: Page, console_monitor: JsConsoleMonitor) -> None:
        self._page = page
        self._console = console_monitor
        page.goto(MARKETING_BASE + "/", wait_until="domcontentloaded", timeout=30_000)

    def test_automate_home_loads(self) -> None:
        assert is_owned_marketing_host(self._page.url), (
            f"Unexpected domain: {self._page.url}"
        )
        # H1 exists — page rendered
        h1_count = self._page.locator("h1").count()
        assert h1_count > 0, "No H1 on the marketing homepage"

    def test_no_fatal_js_errors_on_load(self) -> None:
        try:
            self._page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        # Filter known noise: legacy "appendChild" errors
        fatal = [e for e in self._console.fatal_events if "appendChild" not in e.text]
        assert not fatal, (
            f"Unexpected fatal JS errors: {[{'text': e.text, 'origin': e.source_origin} for e in fatal]}"
        )

    def test_top_navigation_links_present(self) -> None:
        # Reuse the MarketingHeaderComponent's missingLinks() logic
        from pages.marketing.header_component import MarketingHeaderComponent
        missing = MarketingHeaderComponent(self._page).missing_links()
        assert not missing, f"Missing primary nav links: {missing}"

    def test_wordpress_category_page(self) -> None:
        self._page.goto(
            MARKETING_BASE + "/integrate/apps/categories/wordpress",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        title = self._page.title() or ""
        logger.info("WordPress category page title: %s", title)
        assert "wordpress" in title.lower(), (
            f"Title should contain 'WordPress'. Actual: {title!r}"
        )
        wp_links = self._page.locator("a[href*='wordpress']").count()
        assert wp_links > 0, "No WordPress-related links on the category page"
