"""
Lightweight wait helpers. Most of WaitUtils.java's API becomes
unnecessary in Playwright because every Locator interaction auto-waits.
The remaining responsibilities are:
  - Dismissing post-login modals / user guides / cookie banners
  - Waiting for loaders to disappear (visible class-based overlays)
"""

from __future__ import annotations

import logging

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger(__name__)

_OVERLAY_SELECTORS = [
    "button:has-text('Got it')",
    "button:has-text('Dismiss')",
    "button:has-text('Close')",
    ".modal-close",
    ".overlay-close",
]

_USER_GUIDE_SELECTORS = [
    "button:has-text('Skip')",
    "button:has-text('Skip Tour')",
    "button:has-text('Done')",
    ".shepherd-cancel-icon",
]

_LOADER_SELECTORS = [
    ".loader",
    ".spinner",
    "[class*='loading']",
    ".overlay-loader",
]


def dismiss_overlays(page: Page, per_selector_timeout_ms: int = 1500) -> None:
    """Click any visible overlay-dismiss button. No-op if none present."""
    for sel in _OVERLAY_SELECTORS:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=per_selector_timeout_ms):
                btn.click()
                logger.info("Dismissed overlay via: %s", sel)
        except (PlaywrightTimeoutError, Exception):
            # Either the selector didn't match, the element wasn't visible
            # within the brief budget, or click failed because element
            # vanished mid-action. All three mean "no dismiss needed."
            pass


def complete_user_guide(page: Page, per_selector_timeout_ms: int = 1500) -> None:
    """Click any visible user-guide skip button. No-op if not shown."""
    for sel in _USER_GUIDE_SELECTORS:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=per_selector_timeout_ms):
                btn.click()
                logger.info("User guide dismissed via: %s", sel)
        except (PlaywrightTimeoutError, Exception):
            pass


def wait_for_loader_to_clear(page: Page, timeout_ms: int = 30_000) -> None:
    """
    Wait for any visible loader/spinner overlays to disappear.
    Non-fatal — if no loader appears within the budget, we proceed.
    """
    for sel in _LOADER_SELECTORS:
        try:
            # First check if a loader is currently showing.
            loader = page.locator(sel).first
            if loader.is_visible(timeout=500):
                logger.info("Loader detected via '%s' — waiting to clear", sel)
                loader.wait_for(state="hidden", timeout=timeout_ms)
                logger.info("Loader cleared")
                return
        except (PlaywrightTimeoutError, Exception):
            pass
