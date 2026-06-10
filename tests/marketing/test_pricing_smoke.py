"""
Python port of testing.marketing.smoke.PricingSmokeTest from the Java
project. Phase 4 of the migration plan — second test class migrated
to grow the cross-stack comparison dataset.

Java source: src/test/java/testing/marketing/smoke/PricingSmokeTest.java

Nine assertions covering the flozic.ai/integrate/pricing-plan page:
  1. pageReachesLoadedState
  2. noFatalConsoleErrorsDuringLoad
  3. titleContainsBrandName
  4. atLeastMinimumPlanCardsVisible
  5. atLeastOnePlanCtaVisible
  6. buyCtasPresentForPaidTiers
  7. enterpriseContactCtaPresent
  8. viewAllFeaturesButtonsPresent
  9. billingContentBlocksBothExist
  10. primaryNavLinksAllPresent

Reads the marketing surface (no login). Runs against the live flozic.ai
site by default; override with `MARKETING_BASE_URL` env var.

A fresh page navigation happens per test method, mirroring the Java
@BeforeMethod behaviour. Each test is isolated.
"""

from __future__ import annotations

import logging

import pytest
from playwright.sync_api import Page

from pages.marketing.pricing_page import PricingPage
from utils.config import PRODUCT_NAME
from utils.js_console_monitor import JsConsoleMonitor
from utils.test_category import test_category

logger = logging.getLogger(__name__)

# Pricing pages typically expose 3-4 tiers; require at least 2.
MIN_PLAN_CARDS = 2
# Production has 3 BUY NOW / TRY NOW buttons (Standard, Professional, Business).
MIN_BUY_CTAS = 3
# Production has 1 Enterprise Contact Us CTA.
MIN_ENTERPRISE_CTAS = 1
# Production has 4 "View all Features" expanders (one per tier).
MIN_VIEW_ALL_FEATURES = 4


@test_category(
    type="SMOKE",
    requires_login=False,
    feature="Marketing > Pricing",
)
class TestPricingSmoke:
    """Smoke coverage for the flozic.ai pricing page."""

    @pytest.fixture(autouse=True)
    def open_pricing(
        self,
        page: Page,
        console_monitor: JsConsoleMonitor,
    ) -> PricingPage:
        """
        Navigate to pricing before each test. Mirrors Java @BeforeMethod.

        Depends on console_monitor so it's attached BEFORE navigation —
        otherwise the noFatalConsoleErrorsDuringLoad test would miss
        errors that fire during the initial page load.
        """
        self._page = page
        self._console = console_monitor
        self._pricing = PricingPage(page).navigate()
        return self._pricing

    def test_page_reaches_loaded_state(self) -> None:
        assert self._pricing.is_loaded(), (
            f"Pricing page failed to reach loaded state. URL: {self._page.url}"
        )

    def test_no_fatal_console_errors_during_load(self) -> None:
        # Allow the page to settle so any deferred scripts have a chance to log.
        self._page.wait_for_load_state("networkidle", timeout=15_000)
        fatal = self._console.fatal_count()
        assert fatal == 0, (
            f"FAIL-severity console errors during pricing load: {self._console.errors}"
        )

    def test_title_contains_brand_name(self) -> None:
        title = self._page.title()
        assert title and PRODUCT_NAME in title, (
            f"Title does not contain '{PRODUCT_NAME}'. Actual: {title!r}"
        )

    def test_at_least_minimum_plan_cards_visible(self) -> None:
        count = self._pricing.plan_card_count()
        logger.info("Pricing tier card count: %d", count)
        assert count >= MIN_PLAN_CARDS, (
            f"Expected at least {MIN_PLAN_CARDS} pricing tier cards. Found: {count}"
        )

    def test_at_least_one_plan_cta_visible(self) -> None:
        count = self._pricing.plan_cta_count()
        logger.info("Plan CTA count: %d", count)
        assert count >= 1, (
            f"Expected at least 1 plan CTA (BUY/TRY/Contact/View). Found: {count}"
        )

    def test_buy_ctas_present_for_paid_tiers(self) -> None:
        count = self._pricing.buy_cta_count()
        logger.info("Buy CTA count: %d", count)
        assert count >= MIN_BUY_CTAS, (
            f"Expected at least {MIN_BUY_CTAS} BUY NOW / TRY NOW buttons "
            f"(Standard, Professional, Business). Found: {count}"
        )

    def test_enterprise_contact_cta_present(self) -> None:
        count = self._pricing.enterprise_contact_count()
        logger.info("Enterprise Contact CTA count: %d", count)
        assert count >= MIN_ENTERPRISE_CTAS, (
            f"Expected at least {MIN_ENTERPRISE_CTAS} Enterprise Contact Us CTA. Found: {count}"
        )

    def test_view_all_features_buttons_present(self) -> None:
        count = self._pricing.view_all_features_count()
        logger.info("View all Features button count: %d", count)
        assert count >= MIN_VIEW_ALL_FEATURES, (
            f"Expected at least {MIN_VIEW_ALL_FEATURES} 'View all Features' buttons "
            f"(one per tier). Found: {count}"
        )

    def test_billing_content_blocks_both_exist(self) -> None:
        assert self._pricing.has_billing_content_blocks(), (
            "Expected both #monthlyContent and #yearlyContent blocks to exist"
        )

    def test_primary_nav_links_all_present(self) -> None:
        missing = self._pricing.header().missing_links()
        assert not missing, f"Missing primary nav links: {missing}"
