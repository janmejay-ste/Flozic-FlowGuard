"""
Python port of testing.marketing.smoke.HomepageSmokeTest from the Java
project. Phase 4 — third test class migrated.

Java source: src/test/java/testing/marketing/smoke/HomepageSmokeTest.java

Six independent assertions against flozic.ai/:
  1. pageReachesLoadedState
  2. noFatalConsoleErrorsDuringLoad
  3. titleContainsBrandName
  4. heroCtaIsVisible
  5. primaryNavLinksAllPresent
  6. emailUsModalPresentInDom
"""

from __future__ import annotations

import logging

import pytest
from playwright.sync_api import Page

from pages.marketing.home_page import HomePage
from utils.config import PRODUCT_NAME
from utils.js_console_monitor import JsConsoleMonitor
from utils.test_category import test_category

logger = logging.getLogger(__name__)


@test_category(
    type="SMOKE",
    requires_login=False,
    feature="Marketing > Homepage",
)
class TestHomepageSmoke:
    """Smoke coverage for the flozic.ai marketing homepage."""

    @pytest.fixture(autouse=True)
    def open_homepage(
        self,
        page: Page,
        console_monitor: JsConsoleMonitor,
    ) -> HomePage:
        self._page = page
        self._console = console_monitor
        self._home = HomePage(page).navigate()
        return self._home

    def test_page_reaches_loaded_state(self) -> None:
        assert self._home.is_loaded(), (
            f"Homepage failed to reach loaded state. URL: {self._page.url}"
        )

    def test_no_fatal_console_errors_during_load(self) -> None:
        self._page.wait_for_load_state("networkidle", timeout=15_000)
        fatal = self._console.fatal_count()
        assert fatal == 0, (
            f"FAIL-severity console errors during homepage load: {self._console.errors}"
        )

    def test_title_contains_brand_name(self) -> None:
        title = self._page.title()
        assert title and PRODUCT_NAME in title, (
            f"Title does not contain '{PRODUCT_NAME}'. Actual: {title!r}"
        )

    def test_hero_cta_is_visible(self) -> None:
        assert self._home.is_hero_cta_visible(), (
            "Hero CTA not visible — none of the defensive selectors matched"
        )

    def test_primary_nav_links_all_present(self) -> None:
        missing = self._home.header().missing_links()
        assert not missing, f"Missing primary nav links: {missing}"

    def test_email_us_modal_present_in_dom(self) -> None:
        assert self._home.is_email_modal_present_in_dom(), (
            "#myModal (Email Us modal) is not in the homepage DOM"
        )
