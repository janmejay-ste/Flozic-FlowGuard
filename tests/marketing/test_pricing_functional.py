"""
Python port of testing.marketing.functional.PricingFunctionalTest.
"""

from __future__ import annotations

import logging

import pytest
from playwright.sync_api import Page

from pages.marketing.pricing_page import PricingPage
from utils.test_category import test_category

logger = logging.getLogger(__name__)


URL_TIMEOUT_MS = 15_000


def _wait_for_url_contains(page: Page, *fragments: str, timeout_ms: int = URL_TIMEOUT_MS) -> None:
    needles = [f.lower() for f in fragments]
    page.wait_for_url(
        lambda url: any(n in (url or "").lower() for n in needles),
        timeout=timeout_ms,
    )


@test_category(
    type="SANITY",
    requires_login=False,
    feature="Marketing > Pricing",
)
class TestPricingFunctional:
    @pytest.fixture(autouse=True)
    def open_pricing(self, page: Page) -> PricingPage:
        self._page = page
        self._pricing = PricingPage(page).navigate()
        assert self._pricing.is_loaded(), "Pricing page failed to load"
        return self._pricing

    def test_first_buy_cta_triggers_flow(self) -> None:
        url_before = self._page.url
        self._pricing.click_first_buy_cta()
        self._page.wait_for_timeout(2_500)
        url_after = self._page.url
        url_changed = url_after != url_before
        reached_auth = any(p in url_after for p in (
            "login", "register", "signup", "accounts.appypie",
            "checkout", "subscribe"
        ))
        assert url_changed or reached_auth, (
            f"BUY CTA produced no observable change. Before: {url_before} After: {url_after}"
        )

    def test_enterprise_contact_opens_calendly_in_new_tab(self) -> None:
        if self._pricing.enterprise_contact_count() == 0:
            pytest.skip("Enterprise Contact CTA not present on pricing page")

        original = self._page
        context = self._page.context
        pages_before = len(context.pages)

        with context.expect_page(timeout=10_000) as new_page_info:
            self._pricing.click_enterprise_contact_cta()
        new_page = new_page_info.value
        try:
            new_page.wait_for_load_state("domcontentloaded", timeout=10_000)
            new_url = new_page.url
        finally:
            new_page.close()

        assert "calendly" in new_url.lower(), (
            f"Enterprise Contact opened a new tab but URL is not Calendly. Actual: {new_url}"
        )

    def test_view_all_features_button_is_clickable(self) -> None:
        if self._pricing.view_all_features_count() == 0:
            pytest.skip("No 'View all Features' buttons present")
        # No exception is the success contract — production toggles in-card state
        self._pricing.click_first_view_all_features()
        logger.info("View all Features click completed without error")

    def test_header_pricing_link_keeps_user_on_pricing(self) -> None:
        self._pricing.header().click_pricing()
        _wait_for_url_contains(self._page, "pricing")
        assert "pricing" in self._page.url, (
            f"Header pricing link routed away from pricing. Final: {self._page.url}"
        )

    def test_header_login_routes_to_auth_domain(self) -> None:
        self._pricing.header().click_login()
        _wait_for_url_contains(self._page, "accounts.appypie", "login")
        url = self._page.url
        assert "accounts.appypie" in url or "/login" in url, (
            f"Header login did not route to auth domain. Final: {url}"
        )
