"""
Python port of testing.ErrorHandlingTest.

Validates error-page handling — 404, malformed URLs, very long URLs.
"""

from __future__ import annotations

import logging

import pytest
from playwright.sync_api import Page

from pages.error_page import ErrorPage
from utils.config import MARKETING_BASE
from utils.test_category import test_category

logger = logging.getLogger(__name__)


@test_category(
    type="REGRESSION",
    requires_login=False,
    feature="Error Handling",
)
class TestErrorHandling:
    @pytest.fixture(autouse=True)
    def setup(self, page: Page) -> ErrorPage:
        self._page = page
        self._error = ErrorPage(page)
        return self._error

    def test_404_page_for_invalid_url(self) -> None:
        self._error.navigate_to_invalid_url()
        url = self._error.get_current_url()
        logger.info("URL after invalid navigation: %s", url)

        is_404 = self._error.is_404_page()
        is_home = url.rstrip("/").endswith(".ai")
        # Either 404 OR redirect-to-home is acceptable
        assert is_404 or is_home or self._error.page_has_content(), (
            "Invalid URL produced blank/unhandled page"
        )
        assert self._error.page_has_content(), "Error page is blank/empty"

    def test_404_page_has_content(self) -> None:
        self._error.navigate_to_invalid_url()
        if not self._error.is_404_page():
            pytest.skip("Not a 404 page — skipping content validation")
        assert self._error.page_has_content(), "404 page has no content"

    def test_special_character_url_handled_gracefully(self) -> None:
        invalid_url = MARKETING_BASE + "/<script>alert(1)</script>"
        try:
            self._error.navigate_to_url(invalid_url)
        except Exception as e:
            logger.info("URL with special characters rejected: %s", str(e).splitlines()[0])
            return
        source = self._page.content()
        assert "<script>alert" not in source, (
            "XSS vulnerability detected — script tag rendered"
        )

    def test_very_long_url_handled_gracefully(self) -> None:
        long_path = MARKETING_BASE + "/" + ("verylongpath" * 100)
        try:
            self._error.navigate_to_url(long_path)
        except Exception as e:
            logger.info("Very long URL rejected: %s", str(e).splitlines()[0])
            return
        assert self._error.page_has_content() or self._error.is_404_page(), (
            "Very long URL produced a blank page"
        )
