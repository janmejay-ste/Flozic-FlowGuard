"""
Error page helper. Python port of Java ErrorPage.
"""

from __future__ import annotations

import logging

from playwright.sync_api import Page

from utils.config import MARKETING_BASE

logger = logging.getLogger(__name__)


class ErrorPage:
    def __init__(self, page: Page) -> None:
        self.page = page

    def navigate_to_invalid_url(self) -> None:
        url = MARKETING_BASE + "/this-page-does-not-exist-xyz123"
        self.page.goto(url, wait_until="domcontentloaded", timeout=30_000)

    def navigate_to_url(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=30_000)

    def is_404_page(self) -> bool:
        body = (self.page.locator("body").inner_text() or "").lower()
        return any(p in body for p in ["404", "not found", "page not found"])

    def is_500_page(self) -> bool:
        body = (self.page.locator("body").inner_text() or "").lower()
        return any(p in body for p in ["500", "internal server error", "server error"])

    def page_has_content(self) -> bool:
        try:
            body = (self.page.locator("body").inner_text() or "").strip()
            return len(body) > 10
        except Exception:
            return False

    def get_page_title(self) -> str:
        return self.page.title() or ""

    def get_current_url(self) -> str:
        return self.page.url

    def has_home_link(self) -> bool:
        for sel in [
            "a[href='/']",
            "a:has-text('Home')",
            "a:has-text('Go Home')",
            "a:has-text('Back')",
            f"a[href*='{MARKETING_BASE.replace('https://', '').replace('http://', '')}']",
        ]:
            if self.page.locator(sel).count() > 0:
                return True
        return False

    def click_home_link(self) -> None:
        for sel in ["a:has-text('Home')", "a:has-text('Go Home')", "a[href='/']"]:
            loc = self.page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=5_000)
                return
        # Fallback: navigate directly
        self.page.goto(MARKETING_BASE, wait_until="domcontentloaded")
