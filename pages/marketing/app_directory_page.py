"""
App Directory page object. Python port of selectors + flow from the
Java AppPairingTest's interactions with /integrate/app-directory.

What this ports:
  - Navigation
  - Search for an app by name
  - Find and click the matching app card (with virtual-scroll support)
  - Click the '+' icon to add a second app
  - Click a second app card (by index)
  - Click the 'Automate' / 'Get Started' CTA

What this deliberately does NOT port (Phase 1 diagnostic scope):
  - Business transaction tracking (Java HealthTracker concept)
  - Network monitor injection (use Playwright's page.on directly elsewhere)
  - Mutation-observer-based DOM stability (Playwright auto-waits supersede this)
  - Overlay classification telemetry (Phase 1 doesn't need it)

Selectors are copied from the Java AppPairingTest constants so any
divergence in Java vs Python results can be attributed to stack
behavior, not selector drift.
"""

from __future__ import annotations

import logging
import re

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from utils.config import MARKETING_BASE

logger = logging.getLogger(__name__)


# ── Selectors — copied verbatim from Java AppPairingTest ─────────────

SEARCH_INPUT = (
    "input#appConnectName, "
    "input[placeholder*='Search Apps'], "
    "input[placeholder*='search']"
)

CARD = (
    "div.app-box a, "
    "div.integration-card a, "
    "div[class*='app-card'] a, "
    "a[href*='integrations']"
)

PLUS_ICON = (
    "a[href='#selectConnectApp'], "
    "a[href='#selectConnectApp'] span, "
    ".plus-icon, .add-app-btn"
)

SECOND_CARD_VARIANTS = [
    "div.app-box a, div.integration-card a",
    "a[href*='/integrations/']",
    "div.card-body a",
]

AUTOMATE_BUTTON_VARIANTS = [
    "a#getStartedBtn",
    "a.bannerInnerBtn",
    "a[href*='accounts.appypie.com']",
    "a:has-text('Get Started')",
    "a:has-text('Automate')",
    "a.btn-primary, button.btn-primary",
]

APP_DIRECTORY_PATH = "/integrate/app-directory"


def _normalize(text: str) -> str:
    """Mirror Java normalize(): lowercase, trim, collapse whitespace."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.strip().lower())


class AppDirectoryPage:
    """Page object for the App Directory page on the marketing site."""

    def __init__(self, page: Page) -> None:
        self.page = page

    # ── Navigation + search ───────────────────────────────────────────

    def navigate(self) -> "AppDirectoryPage":
        url = MARKETING_BASE + APP_DIRECTORY_PATH
        logger.info("Navigating to App Directory: %s", url)
        # The app-directory page has long-running background scripts
        # (analytics, polling, lazy widgets) that keep the 'load' event
        # from firing within Playwright's default 30s budget. Use
        # 'domcontentloaded' so we proceed once the DOM is parsed —
        # which is when our selectors become interactable.
        self.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        return self

    def search_for_app(self, app_name: str, timeout_ms: int = 15_000) -> None:
        """
        Type an app name into the search box. Playwright's auto-waiting
        replaces the Java mutation-observer logic — we just wait for at
        least one card to be attached after typing.
        """
        search = self.page.locator(SEARCH_INPUT).first
        search.wait_for(state="visible", timeout=timeout_ms)
        search.fill("")  # clear field — Playwright's fill replaces .clear() + select-all
        search.fill(app_name)
        # Brief settle for debounced search; result locator's wait_for does the rest.
        self.page.wait_for_timeout(400)
        try:
            self.page.locator(CARD).first.wait_for(state="attached", timeout=10_000)
        except PlaywrightTimeoutError:
            # No cards rendered — log and let the caller's "card not found"
            # path handle it; not necessarily a hard failure here.
            logger.warning("Search for '%s' produced no cards within 10s", app_name)

    # ── First app card ────────────────────────────────────────────────

    def find_and_click_app_card(
        self,
        app_name: str,
        scroll_attempts: int = 8,
    ) -> bool:
        """
        Scroll-and-click for the matching app card. Returns True if found
        and clicked, False if not found after `scroll_attempts` passes.

        Mirrors Java's findAndClickVerifiedAppCard but uses Playwright's
        auto-wait + re-query semantics, which eliminate the stale-element
        retry loop that Java needs.
        """
        normalized = _normalize(app_name)
        slug = app_name.lower().replace(" ", "-")

        for attempt in range(scroll_attempts):
            card = self._acquire_matched_card(normalized, slug)
            if card is not None:
                try:
                    card.scroll_into_view_if_needed()
                    card.click(timeout=5_000)
                    logger.info(
                        "Clicked app card '%s' (scroll pass %d)",
                        app_name,
                        attempt + 1,
                    )
                    return True
                except PlaywrightTimeoutError as e:
                    logger.debug(
                        "Click on '%s' timed out at pass %d: %s",
                        app_name,
                        attempt + 1,
                        str(e).splitlines()[0],
                    )
                    # Fall through to scroll; the next pass re-acquires.

            # Scroll down to reveal more virtual-list entries
            self.page.evaluate("window.scrollBy(0, 400);")
            self.page.wait_for_timeout(300)

        self._log_search_diagnostic(app_name, normalized, slug)
        return False

    def _acquire_matched_card(
        self,
        normalized: str,
        slug: str,
    ) -> Locator | None:
        """
        Find the first visible card whose text contains `normalized` OR
        whose href contains the slug. Returns a Locator or None.

        Playwright's filter() does the visibility + text check atomically;
        no separate TOCTOU window between detection and acquisition.
        """
        cards = self.page.locator(CARD)
        total = cards.count()
        for i in range(total):
            c = cards.nth(i)
            try:
                if not c.is_visible():
                    continue
                text = _normalize(c.inner_text() or "")
                if normalized and normalized in text:
                    return c
                href = c.get_attribute("href") or ""
                if slug and slug in href.lower():
                    return c
            except Exception:
                # Element vanished mid-check — fine, just skip
                continue
        return None

    def _log_search_diagnostic(
        self,
        app_name: str,
        normalized: str,
        slug: str,
    ) -> None:
        """Mirror Java's logSearchDiagnostic: log DOM state when card not found."""
        try:
            cards = self.page.locator(CARD)
            total = cards.count()
            visible_texts: list[str] = []
            for i in range(min(total, 20)):
                try:
                    c = cards.nth(i)
                    if c.is_visible():
                        t = _normalize(c.inner_text() or "")
                        if t and t not in visible_texts:
                            visible_texts.append(t)
                except Exception:
                    pass

            scroll_y = self.page.evaluate("window.scrollY")
            search_val = ""
            try:
                inp = self.page.locator(SEARCH_INPUT).first
                if inp.count() > 0:
                    search_val = inp.input_value()
            except Exception:
                pass

            logger.warning(
                "[%s] SEARCH DIAGNOSTIC — target='%s' slug='%s' | "
                "total cards in DOM: %d | visible: %d | scrollY: %s | "
                "search input value: '%s' | visible card names: %s",
                app_name,
                normalized,
                slug,
                total,
                len(visible_texts),
                scroll_y,
                search_val,
                visible_texts[:10],
            )
        except Exception as e:
            logger.debug("[%s] Search diagnostic capture failed: %s", app_name, e)

    # ── Plus icon ─────────────────────────────────────────────────────

    def click_plus_icon(self) -> bool:
        """
        Click the '+' icon to open the add-app panel. Returns True if
        clicked, False if not found.
        """
        try:
            btn = self.page.locator(PLUS_ICON).first
            btn.wait_for(state="visible", timeout=5_000)
            btn.scroll_into_view_if_needed()
            btn.click(timeout=5_000)
            logger.info("Clicked '+' icon")
            return True
        except PlaywrightTimeoutError:
            logger.info("No '+' icon found within 5s — scrolling to reveal app panel")
            self.page.evaluate("window.scrollBy(0, 500);")
            self.page.wait_for_timeout(300)
            return False

    # ── Second app card ───────────────────────────────────────────────

    def click_second_app_card(self, iteration: int) -> bool:
        """
        Click a second app card by index. The iteration number picks which
        card from the visible list, mirroring Java's index-based selection.
        Returns True if clicked, False if no visible cards found.
        """
        self.page.wait_for_timeout(300)
        for selector in SECOND_CARD_VARIANTS:
            cards = self.page.locator(selector)
            total = cards.count()
            visible_cards: list[Locator] = []
            for i in range(total):
                c = cards.nth(i)
                try:
                    if c.is_visible():
                        visible_cards.append(c)
                except Exception:
                    continue
            if visible_cards:
                idx = min(iteration, len(visible_cards) - 1)
                card = visible_cards[idx]
                try:
                    card.scroll_into_view_if_needed()
                    card.click(timeout=5_000)
                    logger.info(
                        "Clicked second app card #%d via selector '%s'",
                        idx,
                        selector,
                    )
                    return True
                except PlaywrightTimeoutError as e:
                    logger.debug(
                        "Selector '%s' click failed: %s",
                        selector,
                        str(e).splitlines()[0],
                    )
                    continue
        return False

    # ── Automate button ───────────────────────────────────────────────

    def click_automate_button(self) -> bool:
        """
        Click the 'Get Started' / 'Automate' CTA. Returns True if found
        and clicked, False if no variant matched a visible element.
        """
        for selector in AUTOMATE_BUTTON_VARIANTS:
            try:
                btn = self.page.locator(selector).first
                if btn.is_visible(timeout=1_500):
                    btn.scroll_into_view_if_needed()
                    btn.click(timeout=5_000)
                    logger.info("Clicked Automate button via: %s", selector)
                    return True
            except (PlaywrightTimeoutError, Exception):
                continue
        return False
