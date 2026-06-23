"""
Flozic.ai marketing landing page entry points.

Two interfaces:
  1. Hero prompt on https://www.flozic.ai/  — generic 'Build my workflow'
       textarea: #hero-prompt
       button:   #generateButton  (label: 'Build my workflow')
  2. App-specific page on https://www.flozic.ai/integrate/apps/{slug}/integrations
       textarea: #gs-prompt  (id pattern; varies but is the only textarea in .pb-inner)
       button:   #gs-generate  (label: 'Build my {App} workflow')

Both flows: type prompt -> button becomes enabled (loses [disabled] / [aria-disabled='true'])
-> click -> redirect to login -> after login lands on
https://connectcloud.appypie.com/customeditor with the copilot panel open
(.copilot-panel.open.side-left).
"""

from __future__ import annotations

import logging

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger(__name__)

FLOZIC_HOME = "https://www.flozic.ai/"
APP_INTEGRATIONS_URL = "https://www.flozic.ai/integrate/apps/{slug}/integrations"
CONVERSATIONAL_AGENT_URL = "https://www.flozic.ai/agents/conversational/{slug}"

# After login + redirect, the customeditor URL contains this path.
# Accepts both legacy connectcloud.appypie.com and the new loop.flozic.ai host
# (partial migration — both are valid as of now).
CUSTOMEDITOR_URL_PATTERN = "**/customeditor**"


class FlozicLandingPage:
    """Entry-point page object for the flozic.ai prompt-to-workflow UI."""

    def __init__(self, page: Page):
        self.page = page

    # ── Navigation ────────────────────────────────────────────────────────────
    def open_hero(self) -> None:
        """Open the generic hero prompt at https://www.flozic.ai/."""
        logger.info("Navigating to flozic.ai hero: %s", FLOZIC_HOME)
        self.page.goto(FLOZIC_HOME, wait_until="domcontentloaded")

    def open_app(self, app_slug: str) -> None:
        """Open the app-specific integrations page (e.g. 'google-sheets')."""
        url = APP_INTEGRATIONS_URL.format(slug=app_slug)
        logger.info("Navigating to flozic.ai app page: %s", url)
        self.page.goto(url, wait_until="domcontentloaded")

    def open_conversational_agent(self, agent_slug: str) -> None:
        """Open a conversational-agent page (e.g. 'telegram-bot')."""
        url = CONVERSATIONAL_AGENT_URL.format(slug=agent_slug)
        logger.info("Navigating to flozic.ai conversational-agent page: %s", url)
        self.page.goto(url, wait_until="domcontentloaded")

    # ── Prompt submission ─────────────────────────────────────────────────────
    def submit_prompt(self, prompt: str, timeout_ms: int = 20_000) -> None:
        """
        Type the prompt into the .pb-inner textarea, wait for the build button
        to become enabled, then click it.

        Handles both hero (#hero-prompt / #generateButton) and app-specific
        (#gs-prompt / #gs-generate) markup via a shared .pb-inner container.
        """
        # The textarea is the only <textarea> inside .pb-inner on both pages.
        textarea = self.page.locator(".pb-inner textarea").first
        textarea.wait_for(state="visible", timeout=timeout_ms)
        textarea.click()
        textarea.fill(prompt)
        logger.info("Prompt typed (%d chars).", len(prompt))

        # Button starts with [disabled] + [aria-disabled='true']. Once the
        # textarea has content the page removes both. Wait for enabled state.
        build_btn = self.page.locator(".pb-inner .pb-btn").first
        build_btn.wait_for(state="visible", timeout=timeout_ms)

        # Poll for the button to become enabled (max 8s — UI typically reacts
        # within a single tick of typing, but slow connections may need more).
        deadline = 8_000
        step = 200
        waited = 0
        while waited < deadline:
            is_disabled = build_btn.get_attribute("disabled") is not None
            aria_disabled = build_btn.get_attribute("aria-disabled") == "true"
            if not is_disabled and not aria_disabled:
                break
            self.page.wait_for_timeout(step)
            waited += step
        else:
            # Button never enabled — try clicking anyway; a stale flag may have
            # been left behind by the page script.
            logger.warning(
                "Build button still appears disabled after %dms — clicking anyway.",
                deadline,
            )

        # Capture the label BEFORE click — clicking navigates the page and
        # detaches the button, so any post-click read on it would time out.
        try:
            label = (build_btn.text_content(timeout=2_000) or "").strip()
        except Exception:
            label = "<unknown>"
        build_btn.click()
        logger.info("Build button clicked. Label: '%s'", label)

    # ── Post-submit wait ──────────────────────────────────────────────────────
    def wait_for_customeditor(self, timeout_ms: int = 180_000) -> None:
        """
        After clicking the build button the page goes through login (handled
        externally by perform_login) and then lands on customeditor. Wait for
        the customeditor URL.
        """
        self.page.wait_for_url(CUSTOMEDITOR_URL_PATTERN, timeout=timeout_ms)
        logger.info("Landed on customeditor: %s", self.page.url)
