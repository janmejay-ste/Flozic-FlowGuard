"""
Marketing homepage page object. Python port of Java `HomePage` (P1).

Selectors mirror Java exactly. The hero-CTA and Email-modal selectors
are kept identical so any divergence between the two stacks' assertions
can be attributed to Playwright vs Selenium, not to selector drift.
"""

from __future__ import annotations

import logging

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from pages.marketing.header_component import MarketingHeaderComponent
from utils.config import MARKETING_BASE

logger = logging.getLogger(__name__)


# ── Selectors — kept aligned with Java HomePage constants ────────────

HERO_CTA = (
    "section a[href*='/register'], "
    "section a[href*='/signup'], "
    "div[class*='hero'] a[href*='/register'], "
    "div[class*='hero'] a[href*='/signup'], "
    "a[class*='hero'][href*='/register'], "
    "a.btn-primary[href*='/register'], "
    "a.cta-primary[href*='/register']"
)

DOC_READY_SIGNAL = "h1, [class*='hero'] h1, main h1"

EMAIL_MODAL = "#myModal"
EMAIL_MODAL_TITLE = "#myModal .modal-title"
EMAIL_MODAL_BODY = "#myModal .modal-body"
EMAIL_MODAL_FORM = "#myModal .wpcf7, #myModal form, #myModal [id^='wpcf7-']"
EMAIL_MODAL_TRIGGER = (
    "[data-target='#myModal'], "
    "[data-bs-target='#myModal'], "
    "a[href='#myModal'], "
    "[onclick*='myModal']"
)


class HomePage:
    def __init__(self, page: Page) -> None:
        self.page = page
        self._header = MarketingHeaderComponent(page)

    # ── Navigation + readiness ────────────────────────────────────────

    def navigate(self) -> "HomePage":
        url = MARKETING_BASE + "/"
        logger.info("Navigating to homepage: %s", url)
        self.page.goto(url)
        return self

    def is_loaded(self, timeout_ms: int = 20_000) -> bool:
        """Returns False on timeout; mirrors Java try/except semantics."""
        try:
            self.page.wait_for_load_state("load", timeout=timeout_ms)
            self.page.locator(DOC_READY_SIGNAL).first.wait_for(
                state="attached", timeout=5_000
            )
            return True
        except PlaywrightTimeoutError as e:
            logger.warning("Homepage failed to reach loaded state: %s", e)
            return False

    # ── Hero CTA ──────────────────────────────────────────────────────

    def is_hero_cta_visible(self) -> bool:
        """True if at least one hero-CTA selector matches a visible element."""
        loc = self.page.locator(HERO_CTA)
        total = loc.count()
        for i in range(total):
            try:
                if loc.nth(i).is_visible():
                    return True
            except Exception:
                continue
        return False

    # ── Email Us modal ────────────────────────────────────────────────

    def is_email_modal_present_in_dom(self) -> bool:
        """True if #myModal exists in the DOM (visible or hidden)."""
        return self.page.locator(EMAIL_MODAL).count() > 0

    def is_email_modal_trigger_present(self) -> bool:
        return self.page.locator(EMAIL_MODAL_TRIGGER).count() > 0

    def is_email_modal_visible(self) -> bool:
        """True if the modal is currently displayed (after a trigger click)."""
        loc = self.page.locator(EMAIL_MODAL).first
        if loc.count() == 0:
            return False
        try:
            cls = loc.get_attribute("class") or ""
            style = loc.get_attribute("style") or ""
            return loc.is_visible() and ("show" in cls or "display: block" in style)
        except Exception:
            return False

    def get_email_modal_title(self) -> str:
        loc = self.page.locator(EMAIL_MODAL_TITLE).first
        if loc.count() == 0:
            return ""
        try:
            return (loc.inner_text() or "").strip()
        except Exception:
            return ""

    def email_modal_has_contact_form(self) -> bool:
        return self.page.locator(EMAIL_MODAL_FORM).count() > 0

    def open_email_modal(self, timeout_ms: int = 5_000) -> None:
        """
        Click any visible #myModal trigger. Falls back to invoking the modal
        via jQuery directly when no natural trigger is in the DOM (matches
        the Java helper's last-resort behaviour).
        """
        triggers = self.page.locator(EMAIL_MODAL_TRIGGER)
        total = triggers.count()
        clicked = False
        for i in range(total):
            t = triggers.nth(i)
            try:
                if t.is_visible():
                    t.scroll_into_view_if_needed()
                    t.click(timeout=3_000)
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            logger.warning("No standard data-target trigger for #myModal; invoking via jQuery")
            self.page.evaluate(
                "if (window.jQuery) { jQuery('#myModal').modal('show'); }"
            )
        # Wait for the modal to become visible
        try:
            self.page.wait_for_function(
                """
                () => {
                    const m = document.querySelector('#myModal');
                    if (!m) return false;
                    const cls = m.className || '';
                    const style = m.getAttribute('style') || '';
                    return cls.includes('show') || style.includes('display: block');
                }
                """,
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError:
            logger.warning("Email modal did not become visible within %d ms", timeout_ms)

    def close_email_modal(self) -> None:
        close_btn = self.page.locator(
            "#myModal button[data-dismiss='modal'], "
            "#myModal button[data-bs-dismiss='modal'], "
            "#myModal .close"
        ).first
        if close_btn.count() > 0:
            try:
                close_btn.click(timeout=3_000)
            except Exception:
                try:
                    close_btn.evaluate("el => el.click()")
                except Exception:
                    pass
        # Wait for modal to hide
        try:
            self.page.wait_for_function(
                """
                () => {
                    const m = document.querySelector('#myModal');
                    if (!m) return true;
                    const cls = m.className || '';
                    const style = m.getAttribute('style') || '';
                    return !cls.includes('show') && !style.includes('display: block');
                }
                """,
                timeout=3_000,
            )
        except PlaywrightTimeoutError:
            logger.warning("Email modal did not close within 3 s")

    def click_hero_cta(self) -> None:
        """Click the hero CTA. Used by Functional tests to verify routing."""
        loc = self.page.locator(HERO_CTA)
        total = loc.count()
        for i in range(total):
            c = loc.nth(i)
            try:
                if c.is_visible():
                    c.scroll_into_view_if_needed()
                    c.click(timeout=5_000)
                    return
            except Exception:
                continue
        raise RuntimeError("No visible hero CTA found")

    # ── Component accessors ───────────────────────────────────────────

    def header(self) -> MarketingHeaderComponent:
        return self._header
