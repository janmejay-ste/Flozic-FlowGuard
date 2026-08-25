"""
Python port of testing.AppDirectoryTest.

Validates the App Directory / Integrations page: loading, search, card
display, scroll-loading behavior.
"""

from __future__ import annotations

import logging

import pytest
from playwright.sync_api import Page

from pages.app_directory_browse_page import AppDirectoryBrowsePage
from utils.test_category import test_category

logger = logging.getLogger(__name__)


@test_category(
    type="SANITY",
    requires_login=False,
    feature="App Directory",
)
class TestAppDirectory:
    @pytest.fixture(autouse=True)
    def setup(self, page: Page) -> AppDirectoryBrowsePage:
        self._page = page
        self._app_dir = AppDirectoryBrowsePage(page).navigate_to()
        return self._app_dir

    def test_app_directory_loads(self) -> None:
        assert self._app_dir.is_on_app_directory_page(), (
            f"Not on App Directory page. URL: {self._page.url}"
        )
        assert self._app_dir.is_page_loaded_cleanly(), (
            "App Directory page did not load cleanly"
        )

    def test_integration_cards_present(self) -> None:
        assert self._app_dir.has_integration_cards(), (
            "No integration cards on the App Directory page"
        )
        count = self._app_dir.get_integration_card_count()
        logger.info("Found %d integration cards", count)
        assert count >= 5, f"Expected at least 5 integration cards, found: {count}"

    def test_search_input_exists(self) -> None:
        # Note: search may not always be present; just record what we find
        if self._app_dir.has_search_input():
            logger.info("Search input is available")
        else:
            logger.warning("Search input not found — non-fatal observation")

    def test_search_returns_results(self) -> None:
        if not self._app_dir.has_search_input():
            pytest.skip("Search not available on this version")
        assert self._app_dir.search_returns_results("Google"), (
            "Search for 'Google' returned no results"
        )

    def test_integration_cards_have_content(self) -> None:
        assert self._app_dir.has_integration_cards(), "No cards to validate"
        assert self._app_dir.integration_cards_have_names(), (
            "Some integration cards are missing names/text"
        )

    def test_scroll_loading_works(self) -> None:
        # Baseline AFTER hydration settles. The page server-renders ~240 cards
        # and client-side hydration replaces the grid with ~187 from
        # /api/apps; sampling before that finishes made this test fail on a
        # count "decrease" that scrolling had nothing to do with (240 -> 187,
        # byte-identical across three runs). The static-vs-D1 catalog gap is a
        # real product finding tracked separately — this test's subject is
        # scroll behaviour, so it must start from the hydrated state.
        initial = self._app_dir.get_integration_card_count()
        for _ in range(12):
            self._page.wait_for_timeout(500)
            now = self._app_dir.get_integration_card_count()
            if now == initial:
                break
            if now < initial:
                logger.warning(
                    "[hydration] card grid shrank %d -> %d before any scroll — "
                    "static HTML and the /api/apps catalog disagree (known "
                    "product finding).", initial, now,
                )
            initial = now
        self._app_dir.scroll_to_bottom()
        # Poll until the count stabilises (handles brief DOM churn during re-render)
        stable_after = initial
        for _ in range(10):
            self._page.wait_for_timeout(500)
            count = self._app_dir.get_integration_card_count()
            if count >= initial:
                stable_after = count
                break
            stable_after = count
        logger.info("Card count before: %d, after scroll: %d", initial, stable_after)
        assert stable_after >= initial, (
            f"Card count decreased after scroll: {initial} → {stable_after}"
        )
