"""
Page object for the App Directory page used by AppDirectoryTest
(the sanity-test surface, distinct from AppPairingTest's pairing flow).

Java equivalent: pages.AppDirectoryPage (browse-and-search smoke surface).
"""

from __future__ import annotations

import logging

from playwright.sync_api import Page

from utils.config import integrate_path

logger = logging.getLogger(__name__)


CARD_SELECTORS = (
    "div.app-box, "
    "div.integration-card, "
    "div[class*='app-card'], "
    "a[href*='/integrate/apps/']"
)

SEARCH_INPUT = (
    "input#appConnectName, "
    "input[placeholder*='Search Apps' i], "
    "input[placeholder*='search' i]"
)


class AppDirectoryBrowsePage:
    def __init__(self, page: Page) -> None:
        self.page = page

    def navigate_to(self) -> "AppDirectoryBrowsePage":
        url = integrate_path("app-directory")
        logger.info("Navigating to App Directory: %s", url)
        self.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        return self

    def is_on_app_directory_page(self) -> bool:
        return "app-directory" in self.page.url

    def is_page_loaded_cleanly(self) -> bool:
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=20_000)
            return True
        except Exception:
            return False

    def has_integration_cards(self) -> bool:
        return self.page.locator(CARD_SELECTORS).count() > 0

    def get_integration_card_count(self) -> int:
        return self.page.locator(CARD_SELECTORS).count()

    def has_search_input(self) -> bool:
        return self.page.locator(SEARCH_INPUT).count() > 0

    def search_for_integration(self, term: str) -> None:
        loc = self.page.locator(SEARCH_INPUT).first
        loc.fill("")
        loc.fill(term)
        self.page.wait_for_timeout(800)

    def search_returns_results(self, term: str) -> bool:
        self.search_for_integration(term)
        # Allow render to settle
        self.page.wait_for_timeout(500)
        return self.get_integration_card_count() > 0

    def integration_cards_have_names(self) -> bool:
        cards = self.page.locator(CARD_SELECTORS)
        total = min(cards.count(), 20)
        for i in range(total):
            try:
                text = (cards.nth(i).inner_text() or "").strip()
                if not text:
                    return False
            except Exception:
                continue
        return True

    def scroll_to_bottom(self) -> None:
        self.page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
        self.page.wait_for_timeout(500)
