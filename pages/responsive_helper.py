"""
Viewport / responsive-layout helpers. Python port of Java ResponsiveHelper.

Playwright handles viewport sizing natively via context.set_viewport_size
(or page.set_viewport_size for an existing page). This wrapper keeps the
API shape similar to the Java helper so the test code reads similarly.
"""

from __future__ import annotations

import logging

from playwright.sync_api import Page

logger = logging.getLogger(__name__)


MOBILE = (375, 667)
MOBILE_SMALL = (320, 568)
TABLET = (768, 1024)
DESKTOP = (1440, 900)
DESKTOP_LARGE = (1920, 1080)


class ResponsiveHelper:
    def __init__(self, page: Page) -> None:
        self.page = page
        # Capture original size so restore_original_size can return to it
        self._original: tuple[int, int] | None = None
        size = page.viewport_size
        if size:
            self._original = (size["width"], size["height"])

    def save_original_size(self) -> None:
        size = self.page.viewport_size
        if size:
            self._original = (size["width"], size["height"])

    def restore_original_size(self) -> None:
        if self._original:
            self.page.set_viewport_size({"width": self._original[0], "height": self._original[1]})

    # ── Setters ───────────────────────────────────────────────────────

    def set_mobile_viewport(self) -> None:
        self.page.set_viewport_size({"width": MOBILE[0], "height": MOBILE[1]})

    def set_mobile_small_viewport(self) -> None:
        self.page.set_viewport_size({"width": MOBILE_SMALL[0], "height": MOBILE_SMALL[1]})

    def set_tablet_viewport(self) -> None:
        self.page.set_viewport_size({"width": TABLET[0], "height": TABLET[1]})

    def set_desktop_viewport(self) -> None:
        self.page.set_viewport_size({"width": DESKTOP[0], "height": DESKTOP[1]})

    def set_desktop_large_viewport(self) -> None:
        self.page.set_viewport_size({"width": DESKTOP_LARGE[0], "height": DESKTOP_LARGE[1]})

    # ── Predicates ────────────────────────────────────────────────────

    def is_mobile_viewport(self) -> bool:
        s = self.page.viewport_size
        return bool(s) and s["width"] <= 480

    def is_tablet_viewport(self) -> bool:
        s = self.page.viewport_size
        return bool(s) and 481 <= s["width"] <= 1024

    def is_desktop_viewport(self) -> bool:
        s = self.page.viewport_size
        return bool(s) and s["width"] >= 1025

    def is_element_visible(self, selector: str) -> bool:
        loc = self.page.locator(selector).first
        if loc.count() == 0:
            return False
        try:
            return loc.is_visible()
        except Exception:
            return False

    def is_element_hidden(self, selector: str) -> bool:
        loc = self.page.locator(selector).first
        if loc.count() == 0:
            return True
        try:
            return not loc.is_visible()
        except Exception:
            return True

    def has_horizontal_scroll(self) -> bool:
        return bool(self.page.evaluate(
            "() => document.documentElement.scrollWidth > document.documentElement.clientWidth"
        ))

    def has_mobile_menu(self) -> bool:
        """Check for a hamburger / mobile-menu toggle."""
        sels = [
            "button[aria-label*='menu' i]",
            ".navbar-toggler",
            ".hamburger",
            "[class*='mobile-menu']",
            "button.menu-toggle",
        ]
        for s in sels:
            loc = self.page.locator(s).first
            try:
                if loc.count() > 0 and loc.is_visible():
                    return True
            except Exception:
                pass
        return False

    def element_fits_viewport(self, selector: str) -> bool:
        """True iff element's bounding box fits within the viewport width."""
        loc = self.page.locator(selector).first
        if loc.count() == 0:
            return False
        try:
            box = loc.bounding_box()
            size = self.page.viewport_size
            if not box or not size:
                return False
            return box["x"] >= 0 and (box["x"] + box["width"]) <= size["width"]
        except Exception:
            return False

    def images_are_responsive(self) -> bool:
        """All visible images fit within the viewport horizontally."""
        return bool(self.page.evaluate(
            """
            () => {
                const vw = document.documentElement.clientWidth;
                const imgs = Array.from(document.querySelectorAll('img'));
                return imgs.every(img => {
                    if (img.offsetParent === null) return true;  // not visible
                    const r = img.getBoundingClientRect();
                    return r.width <= vw;
                });
            }
            """
        ))
