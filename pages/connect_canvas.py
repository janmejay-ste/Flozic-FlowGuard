"""
Connect-canvas helpers — wait for the workflow cards on the right-hand canvas
to be populated with real app names (not placeholder "Select Trigger App" /
"Select Action App" text), then screenshot the canvas for AI validation.

Why this exists:
  The copilot panel says "Connect created!" several seconds before the
  canvas on the right actually updates. If the test logs out immediately
  after the copilot message it captures the workflow in a half-built
  state (placeholders still showing). We need to wait until the cards
  carry the real app names that the copilot announced.

Detection strategy:
  - The empty/placeholder state contains literal text "Select Trigger App"
    or "Select Action App".
  - The populated state replaces that with the chosen app's name.
  - We poll for the placeholder text to DISAPPEAR (and for the expected
    trigger app name to APPEAR, as a positive signal).
"""

from __future__ import annotations

import logging
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger(__name__)

# Placeholder text shown on unpopulated canvas cards.
PLACEHOLDER_TEXTS = ("Select Trigger App", "Select Action App")

# Selector for the canvas region — we screenshot this rather than the full
# page so the AI sees only the relevant nodes. Fallbacks for layout drift.
CANVAS_SELECTORS = (
    "app-custom-editor",
    ".canvas-container",
    "[class*='customeditor']",
)


def _canvas_locator(page: Page):
    """Pick the first available canvas-region locator."""
    for sel in CANVAS_SELECTORS:
        loc = page.locator(sel).first
        try:
            if loc.is_visible(timeout=500):
                return loc
        except Exception:
            continue
    # Last-resort: full page.
    return page.locator("body").first


def wait_for_canvas_populated(
    page: Page,
    expected_trigger: str,
    total_timeout_ms: int = 90_000,
    poll_interval_ms: int = 5_000,
) -> bool:
    """
    Poll the canvas every `poll_interval_ms` for up to `total_timeout_ms`.
    Return True when:
      - the expected trigger app name appears in the canvas region, AND
      - neither "Select Trigger App" nor "Select Action App" placeholder
        text is visible in the canvas region.

    Return False on timeout. Does NOT raise — caller decides whether
    a timeout is fatal.
    """
    canvas = _canvas_locator(page)
    waited = 0
    while waited < total_timeout_ms:
        try:
            text = (canvas.text_content(timeout=2_000) or "")
        except (PlaywrightTimeoutError, Exception):
            text = ""

        has_trigger     = expected_trigger.lower() in text.lower()
        has_placeholder = any(p in text for p in PLACEHOLDER_TEXTS)

        if has_trigger and not has_placeholder:
            logger.info(
                "Canvas populated after %.1fs (trigger '%s' visible, no placeholders).",
                waited / 1000, expected_trigger,
            )
            return True

        logger.debug(
            "Canvas not yet populated (%.0fs). trigger_visible=%s, "
            "placeholder_visible=%s. Polling again.",
            waited / 1000, has_trigger, has_placeholder,
        )
        page.wait_for_timeout(poll_interval_ms)
        waited += poll_interval_ms

    logger.warning(
        "Canvas did not finish populating within %ds. "
        "Final text contains trigger=%s, placeholder=%s.",
        total_timeout_ms // 1000,
        expected_trigger.lower() in text.lower(),
        any(p in text for p in PLACEHOLDER_TEXTS),
    )
    return False


def screenshot_canvas(page: Page, output_path: Path) -> Path:
    """
    Take a PNG screenshot scoped to the canvas region (or full page if the
    canvas locator isn't available). Returns the saved path. Creates the
    parent directory if needed.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas = _canvas_locator(page)
    try:
        # Locator-level screenshot — captures only the canvas region.
        canvas.screenshot(path=str(output_path), timeout=10_000)
        logger.info("Canvas screenshot saved: %s", output_path)
    except (PlaywrightTimeoutError, Exception) as e:
        # Fall back to full-page screenshot if the locator screenshot fails.
        logger.warning(
            "Canvas-locator screenshot failed (%s); falling back to full-page.",
            str(e).splitlines()[0],
        )
        page.screenshot(path=str(output_path), full_page=False)
    return output_path
