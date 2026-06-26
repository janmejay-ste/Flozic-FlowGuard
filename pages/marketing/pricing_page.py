"""
Pricing page object. Python port of the Java `PricingPage`.

Selectors mirror Java exactly so any divergence between the two
stacks' assertions can be attributed to Playwright vs Selenium
behaviour rather than to selector-list drift.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from typing import Any

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from pages.marketing.header_component import MarketingHeaderComponent
from utils.config import integrate_path

logger = logging.getLogger(__name__)

# Plan names testable via the TRY NOW flow. Enterprise has a separate
# Contact-Us CTA that opens Calendly — covered by click_enterprise_contact_cta.
TRY_NOW_PLAN_NAMES = ("Standard", "Professional", "Business")


# ── Modal categories returned by PlanChangeService after a TRY NOW click ──
#
# See docs/plan-change-matrix for the full decision table. The page object
# uses a cheap keyword-based classifier; the AI validator (utils/
# ai_popup_validator.py) is the second opinion that confirms the popup is
# correct for the (current_plan, target_plan, period) tuple.

POPUP_CATEGORIES = (
    "NO_POPUP",              # Free → any paid: proceeds straight to checkout
    "CONFIRM_TRIAL",         # On trial → upgrade target
    "CONFIRM_PAID",          # On paid → upgrade target
    "CONFIRM_SWITCH_YEARLY", # Monthly → Yearly switch (same tier)
    "BLOCK",                 # Downgrade not allowed
    "BLOCK_PERIOD",          # Yearly → Monthly not allowed
    "SAME",                  # Already on this exact plan
    "CONTACT_US",            # Enterprise target → Calendly
    "UNKNOWN",               # Classifier couldn't categorize
)


@dataclass
class PopupSnapshot:
    """Structured contents of the .pcc-modal that PlanChangeService renders
    after a TRY NOW click. All fields default to empty so a missing modal
    is still representable (category_guess='NO_POPUP')."""
    title:            str = ""
    message:          str = ""
    primary_action:   str = ""
    secondary_action: str = ""
    category_guess:   str = "UNKNOWN"
    current_url:      str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _classify_popup(title: str, message: str) -> str:
    """Keyword-based category guess. AI validator is the source of truth;
    this is just a hint for the prompt."""
    t = f"{title} {message}".lower()
    if "switching from a yearly to a monthly" in t and "not allowed" in t:
        return "BLOCK_PERIOD"
    if "downgrad" in t and "not allowed" in t:
        return "BLOCK"
    if "already your current" in t or "same plan" in t:
        return "SAME"
    if "switch to yearly" in t:
        return "CONFIRM_SWITCH_YEARLY"
    if "end your trial" in t and "activate the new plan" in t:
        return "CONFIRM_TRIAL"
    if "cancel your current plan" in t and "activate the new plan" in t:
        return "CONFIRM_PAID"
    if "let's talk" in t or "contact us" in t or "calendly" in t:
        return "CONTACT_US"
    return "UNKNOWN"


def _safe_text(locator, timeout_ms: int = 3_000) -> str:
    try:
        return (locator.inner_text(timeout=timeout_ms) or "").strip()
    except Exception:
        return ""


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

    # ── TRY NOW per-plan flow (PlanChangeService popup capture) ───────

    def switch_to_period(self, period: str, timeout_ms: int = 10_000) -> None:
        """Toggle the billing-period radio: "Yearly" (default checked) or
        "Monthly". The radio inputs are visually hidden (styled via their
        <label> siblings) so we click the label, not the input.

        The page swaps card content client-side — no URL change. We wait
        for the relevant CTA text ("TRY NOW" yearly / "BUY NOW" monthly)
        to be visible as proof the swap completed.
        """
        if period not in ("Monthly", "Yearly"):
            raise ValueError(f"Unknown period {period!r}; expected Monthly or Yearly.")

        # Ensure the toggle is in view before clicking — same below-fold issue
        # as the plan cards.
        self.scroll_to_pricing_section()

        radio_id = "switchMonthly" if period == "Monthly" else "switchYearly"
        # Check first if already in this state — no-op if so.
        try:
            already = self.page.locator(f"#{radio_id}").is_checked()
        except Exception:
            already = False
        if already:
            logger.info("[pricing] Period already %s — no toggle needed.", period)
            return

        label = self.page.locator(f"label[for='{radio_id}']").first
        label.scroll_into_view_if_needed()
        label.click(timeout=timeout_ms)
        logger.info("[pricing] Toggled period to: %s", period)

        # Wait for the corresponding CTA text to appear on a visible card.
        expected_cta_text = "BUY NOW" if period == "Monthly" else "TRY NOW"
        cta_proof = self.page.locator(
            f"a.upgrd-btn:visible:has-text('{expected_cta_text}')"
        ).first
        try:
            cta_proof.wait_for(state="visible", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            logger.warning(
                "[pricing] After switching to %s, no visible %s button "
                "appeared within %dms. The toggle may not have taken effect.",
                period, expected_cta_text, timeout_ms,
            )

    def scroll_to_pricing_section(self, timeout_ms: int = 10_000) -> None:
        """Scroll the Yearly/Monthly tabs into view. The page-hero section
        pushes the plan cards below the fold; without scrolling, Playwright
        will find DOM matches but they may still be in inactive (hidden)
        carousel/tab sections."""
        anchor_selectors = (
            "li:has-text('Yearly Pricing')",
            "a:has-text('Yearly Pricing')",
            ".pricing-tab",
            "h2:has-text('Choose the plan')",
        )
        for sel in anchor_selectors:
            try:
                anchor = self.page.locator(sel).first
                if anchor.count() > 0:
                    anchor.scroll_into_view_if_needed(timeout=timeout_ms)
                    logger.info("[pricing] Scrolled to anchor: %s", sel)
                    return
            except Exception:
                continue
        # Fallback — just scroll halfway down the page so cards are reachable.
        self.page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
        logger.info("[pricing] Fallback scroll: scrolled to half page height.")

    def click_try_now_for(self, plan_name: str, timeout_ms: int = 20_000) -> None:
        """Click TRY NOW on the visible card whose .Premium-paln contains an
        h3 with exactly `plan_name`. Yearly is the default tab so no toggle
        needed.

        The page duplicates plan cards across hidden Monthly/Yearly +
        Automate/Agent + carousel variants — we MUST filter by :visible or
        the locator resolves to a hidden duplicate and times out. The pricing
        section is also below the page hero, so we scroll first.
        """
        if plan_name not in TRY_NOW_PLAN_NAMES:
            raise ValueError(
                f"Unknown plan {plan_name!r}; expected one of {TRY_NOW_PLAN_NAMES}."
            )

        self.scroll_to_pricing_section()

        # :visible filters out hidden duplicates in inactive tabs/carousels.
        # h3.text-center:text-is — exact match — avoids "All Standard Features"
        # tooltip false-positives.
        card = self.page.locator(
            f".Premium-paln:visible:has(h3.text-center:text-is('{plan_name}'))"
        ).first
        try:
            card.wait_for(state="visible", timeout=timeout_ms)
        except Exception:
            # Diagnostic: how many cards did we see with that h3, hidden + visible?
            total = self.page.locator(
                f".Premium-paln:has(h3.text-center:text-is('{plan_name}'))"
            ).count()
            visible = self.page.locator(
                f".Premium-paln:visible:has(h3.text-center:text-is('{plan_name}'))"
            ).count()
            logger.error(
                "[pricing] No VISIBLE plan card for %r within %dms "
                "(%d total in DOM, %d visible). Check Yearly tab is active.",
                plan_name, timeout_ms, total, visible,
            )
            raise

        # CTA label depends on the active period: "TRY NOW" on Yearly,
        # "BUY NOW" on Monthly. The button is `a.upgrd-btn` in both cases —
        # scoping to the card is enough, no text constraint needed.
        cta = card.locator("a.upgrd-btn").first
        cta.scroll_into_view_if_needed()
        cta.wait_for(state="visible", timeout=timeout_ms)
        cta_text = ""
        try:
            cta_text = (cta.inner_text(timeout=1_500) or "").strip()
        except Exception:
            pass
        cta.click(timeout=10_000)
        logger.info(
            "[pricing] Clicked %s for plan=%s",
            cta_text or "CTA", plan_name,
        )

    def wait_for_popup(self, timeout_ms: int = 60_000) -> PopupSnapshot:
        """Wait for the .pcc-modal to render and return structured contents.

        If no modal appears within `timeout_ms`, returns a snapshot with
        category_guess='NO_POPUP' so the caller can decide whether that was
        expected (Free → checkout) or a regression. Does NOT raise on
        absence — the caller (or AI validator) decides what NO_POPUP means.
        """
        modal = self.page.locator(".pcc-modal").first
        try:
            modal.wait_for(state="visible", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            logger.warning(
                "[pricing] .pcc-modal not visible within %dms. URL: %s",
                timeout_ms, self.page.url,
            )
            return PopupSnapshot(
                category_guess="NO_POPUP",
                current_url=self.page.url or "",
            )

        title    = _safe_text(modal.locator(".pcc-title"))
        message  = _safe_text(modal.locator(".pcc-message"))
        buttons  = modal.locator(".pcc-actions button")
        try:
            n_buttons = buttons.count()
        except Exception:
            n_buttons = 0

        primary = ""
        secondary = ""
        # Convention: secondary on the left (.pcc-btn-secondary), primary on
        # the right (.pcc-btn-primary). Walk indices, classify by class.
        for i in range(n_buttons):
            btn = buttons.nth(i)
            klass = (btn.get_attribute("class") or "")
            label = _safe_text(btn)
            if "pcc-btn-primary" in klass:
                primary = label
            elif "pcc-btn-secondary" in klass:
                secondary = label

        # Fallback: positional if class names missing.
        if not primary and n_buttons >= 1:
            primary = _safe_text(buttons.nth(n_buttons - 1))
        if not secondary and n_buttons >= 2:
            secondary = _safe_text(buttons.nth(0))

        category = _classify_popup(title, message)
        logger.info(
            "[pricing] popup captured: title=%r category=%s primary=%r secondary=%r",
            title, category, primary, secondary,
        )
        return PopupSnapshot(
            title=title, message=message,
            primary_action=primary, secondary_action=secondary,
            category_guess=category,
            current_url=self.page.url or "",
        )

    def close_popup(self, timeout_ms: int = 10_000) -> None:
        """Click the × close button. Silent no-op if no popup is visible."""
        close_btn = self.page.locator("button.pcc-close").first
        try:
            close_btn.wait_for(state="visible", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            logger.info("[pricing] No .pcc-close visible; nothing to close.")
            return
        close_btn.click()
        logger.info("[pricing] Closed popup via × button.")

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
