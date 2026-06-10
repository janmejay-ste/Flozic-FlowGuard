"""
Pricing page object. Python port of the Java `PricingPage`.

Selectors mirror Java exactly so any divergence between the two
stacks' assertions can be attributed to Playwright vs Selenium
behaviour rather than to selector-list drift.
"""

from __future__ import annotations

import logging

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from pages.marketing.header_component import MarketingHeaderComponent
from utils.config import integrate_path

logger = logging.getLogger(__name__)


# ── Selectors — kept aligned with Java PricingPage constants ─────────

# Pricing tier / plan cards. Production uses .carousel_item .wrap_pricing;
# defensive fallbacks for resilience.
PLAN_CARDS = (
    ".carousel_item .wrap_pricing, "
    ".pricing-card, .plan-card, "
    "[class*='pricing-tier'], [class*='plan-tier'], "
    "[class*='pricing-plan'], [class*='price-card'], "
    "[data-plan], [data-tier]"
)

# Primary BUY NOW / TRY NOW CTAs (3 per non-Enterprise tier).
BUY_CTA = "a.upgrd-btn"

# Enterprise tier Contact Us CTA (1, opens Calendly in new tab).
ENTERPRISE_CONTACT_CTA = "a.upgrd-contact"

# View all Features expander (one per tier; mobile-only display).
VIEW_ALL_FEATURES_BTN = "button.viewall-btn"

# Generic plan-CTA union — used by "at least one CTA visible" assertion.
PLAN_CTA_UNION = "a.upgrd-btn, a.upgrd-contact, button.viewall-btn"

# Billing-period content blocks (monthly/yearly toggle targets).
BILLING_CONTENT_BLOCKS = "#monthlyContent, #yearlyContent"

# Readiness signal — any of these means the page is rendered enough to test.
DOC_READY_SIGNAL = "h1, main h1, [class*='pricing'] h1, [class*='pricing-card'], .plan-card"

# Pricing-page slug (full URL constructed via integrate_path()).
SLUG = "pricing-plan"


class PricingPage:
    def __init__(self, page: Page) -> None:
        self.page = page
        self._header = MarketingHeaderComponent(page)

    # ── Navigation + readiness ────────────────────────────────────────

    def navigate(self) -> "PricingPage":
        """Navigate to the pricing page and return self for chaining."""
        url = integrate_path(SLUG)
        logger.info("Navigating to pricing: %s", url)
        self.page.goto(url)
        return self

    def is_loaded(self, timeout_ms: int = 20_000) -> bool:
        """
        Wait for document.readyState=='complete' AND the doc-ready signal
        to be present. Returns False on timeout, matching Java's
        defensive try/except semantics (caller decides how to react).
        """
        try:
            # Playwright's wait_for_load_state covers document.readyState==complete
            self.page.wait_for_load_state("load", timeout=timeout_ms)
            # Then require at least one readiness signal element
            self.page.locator(DOC_READY_SIGNAL).first.wait_for(
                state="attached", timeout=5_000
            )
            return True
        except PlaywrightTimeoutError as e:
            logger.warning("Pricing page failed to reach loaded state: %s", e)
            return False

    # ── Counts (visible-only, except where Java explicitly differs) ───

    def plan_card_count(self) -> int:
        """Count visible plan cards. Java filters to displayed-only — we mirror."""
        return self._count_visible(PLAN_CARDS)

    def plan_cta_count(self) -> int:
        """Sum of buy + enterprise + view-all counts."""
        return (
            self.buy_cta_count()
            + self.enterprise_contact_count()
            + self.view_all_features_count()
        )

    def buy_cta_count(self) -> int:
        """Visible BUY NOW / TRY NOW buttons."""
        return self._count_visible(BUY_CTA)

    def enterprise_contact_count(self) -> int:
        """Visible Enterprise Contact Us CTAs."""
        return self._count_visible(ENTERPRISE_CONTACT_CTA)

    def view_all_features_count(self) -> int:
        """
        View all Features buttons present in the DOM. INTENTIONALLY NOT
        filtered by visibility — these are mobile-only expanders and
        would fail desktop-viewport tests if visibility-filtered.
        Matches the Java behaviour exactly.
        """
        return self.page.locator(VIEW_ALL_FEATURES_BTN).count()

    def has_billing_content_blocks(self) -> bool:
        """True if both #monthlyContent and #yearlyContent exist."""
        return self.page.locator(BILLING_CONTENT_BLOCKS).count() >= 2

    # ── Click actions (used by Functional tests) ──────────────────────

    def click_first_buy_cta(self) -> None:
        """Click the first VISIBLE BUY NOW / TRY NOW CTA.

        Pricing page uses a carousel — the first DOM match may be hidden
        in an off-screen slide. We must select the first *visible* one.
        """
        cta = self.page.locator(f"{BUY_CTA}:visible").first
        cta.scroll_into_view_if_needed()
        cta.click(timeout=10_000)

    def click_enterprise_contact_cta(self) -> None:
        """Click the first visible Enterprise Contact Us CTA."""
        cta = self.page.locator(f"{ENTERPRISE_CONTACT_CTA}:visible").first
        if cta.count() == 0:
            raise RuntimeError("Enterprise Contact CTA not visible on pricing page")
        cta.scroll_into_view_if_needed()
        cta.click(timeout=10_000)

    def click_first_view_all_features(self) -> None:
        """View-all-Features buttons are mobile-only. Force-click via JS so
        desktop viewport tests still validate that the button is wired up."""
        btn = self.page.locator(VIEW_ALL_FEATURES_BTN).first
        if btn.count() == 0:
            raise RuntimeError("View all Features button not found")
        # These buttons are display:none on desktop. Dispatch click directly.
        btn.evaluate("el => el.click()")

    # ── Component accessors ───────────────────────────────────────────

    def header(self) -> MarketingHeaderComponent:
        return self._header

    # ── Helpers ───────────────────────────────────────────────────────

    def _count_visible(self, selector: str) -> int:
        """Count locator matches that are visibly displayed."""
        loc = self.page.locator(selector)
        total = loc.count()
        # is_visible() is an instance method on each Locator. Iterate by index.
        visible = 0
        for i in range(total):
            try:
                if loc.nth(i).is_visible():
                    visible += 1
            except Exception:
                # Element disappeared between count and check — treat as not visible.
                pass
        return visible
