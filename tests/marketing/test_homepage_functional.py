"""
Python port of testing.marketing.functional.HomepageFunctionalTest.

Routing-only assertions: each test clicks one interactive element and
verifies the URL change. No form submissions (matches Java scope).
"""

from __future__ import annotations

import logging

import pytest
from playwright.sync_api import Page

from pages.marketing.home_page import HomePage
from utils.test_category import test_category

logger = logging.getLogger(__name__)


URL_TIMEOUT_MS = 15_000


def _wait_for_url_contains(page: Page, *fragments: str, timeout_ms: int = URL_TIMEOUT_MS) -> None:
    """Wait for the URL to contain ANY fragment (case-insensitive)."""
    needles = [f.lower() for f in fragments]
    page.wait_for_url(
        lambda url: any(n in (url or "").lower() for n in needles),
        timeout=timeout_ms,
    )


@test_category(
    type="SANITY",
    requires_login=False,
    feature="Marketing > Homepage",
)
class TestHomepageFunctional:
    @pytest.fixture(autouse=True)
    def open_homepage(self, page: Page) -> HomePage:
        self._page = page
        self._home = HomePage(page).navigate()
        assert self._home.is_loaded(), "Homepage failed to load before functional test"
        return self._home

    def test_hero_cta_routes_to_signup(self) -> None:
        self._home.click_hero_cta()
        _wait_for_url_contains(self._page, "/register", "/signup")
        url = self._page.url
        assert "/register" in url or "/signup" in url, (
            f"Hero CTA did not route to a signup URL. Final: {url}"
        )

    def test_header_signup_routes_to_register(self) -> None:
        self._home.header().click_signup()
        _wait_for_url_contains(self._page, "/register", "/signup")
        url = self._page.url
        assert "/register" in url or "/signup" in url, (
            f"Header signup did not route correctly. Final: {url}"
        )

    def test_header_login_routes_to_auth_domain(self) -> None:
        self._home.header().click_login()
        _wait_for_url_contains(self._page, "authv2.flozic.ai", "accounts.appypie", "/login")
        url = self._page.url
        assert "authv2.flozic.ai" in url or "accounts.appypie" in url or "/login" in url, (
            f"Header login did not route to auth domain. Final: {url}"
        )

    def test_header_pricing_routes_to_pricing_page(self) -> None:
        self._home.header().click_pricing()
        _wait_for_url_contains(self._page, "pricing")
        assert "pricing" in self._page.url, (
            f"Header pricing did not route correctly. Final: {self._page.url}"
        )

    def test_header_app_directory_routes_correctly(self) -> None:
        self._home.header().click_app_directory()
        _wait_for_url_contains(self._page, "app-directory", "integrations")
        url = self._page.url
        assert "app-directory" in url or "integrations" in url, (
            f"Header app-directory did not route. Final: {url}"
        )

    # ── Email Us modal ────────────────────────────────────────────────

    def test_email_us_modal_can_be_opened(self) -> None:
        self._home.open_email_modal()
        assert self._home.is_email_modal_visible(), (
            "Email Us modal did not become visible after open invocation"
        )

    def test_email_us_modal_title(self) -> None:
        self._home.open_email_modal()
        assert self._home.get_email_modal_title() == "Email Us", (
            f"Email Us modal title mismatch. Got: {self._home.get_email_modal_title()!r}"
        )

    def test_email_us_modal_close_button_dismisses(self) -> None:
        self._home.open_email_modal()
        assert self._home.is_email_modal_visible(), "Modal must open before close test"
        self._home.close_email_modal()
        assert not self._home.is_email_modal_visible(), (
            "Email Us modal did not dismiss after close-button click"
        )

    def test_email_us_modal_contains_contact_form(self) -> None:
        self._home.open_email_modal()
        assert self._home.email_modal_has_contact_form(), (
            "Email Us modal has no Contact Form 7 form container"
        )
