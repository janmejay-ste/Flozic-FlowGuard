"""
Copilot panel on the customeditor page.

After the flozic.ai prompt is submitted and the user is redirected through
login to https://connectcloud.appypie.com/customeditor, the left-side copilot
panel opens automatically. Selector: .copilot-panel.open.side-left

The copilot streams several assistant messages:
  1. Greeting ('Hi <name>! ...')
  2. Echo of the user prompt
  3. 'Building your <pipeline> automation!' (in-progress)
  4. 'Connect created!' followed by Trigger/Action 1/Action 2 lines.

Verification: wait for any assistant message containing 'Connect created'
and confirm the expected trigger app appears somewhere in that message.
"""

from __future__ import annotations

import logging

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger(__name__)

PANEL_SEL = ".copilot-panel.open.side-left"
CONNECT_CREATED_SEL = (
    f"{PANEL_SEL} .copilot-message.assistant .message-content.formatted"
    ":has-text('Connect created')"
)


class CopilotPanel:
    def __init__(self, page: Page):
        self.page = page

    def wait_for_panel_open(self, timeout_ms: int = 30_000) -> None:
        """Wait for the copilot panel itself to be visible and open."""
        self.page.locator(PANEL_SEL).first.wait_for(
            state="visible", timeout=timeout_ms
        )
        logger.info("Copilot panel is open.")

    def wait_for_connect_created(self, timeout_ms: int = 120_000) -> str:
        """
        Wait for the 'Connect created!' assistant message and return its
        full text content. Copilot streaming can take 60-90s for complex
        prompts so the default timeout is generous.
        """
        msg = self.page.locator(CONNECT_CREATED_SEL).first
        msg.wait_for(state="visible", timeout=timeout_ms)
        text = (msg.text_content() or "").strip()
        logger.info("Copilot 'Connect created' message: %s", text.replace("\n", " | "))
        return text

    def verify_trigger(self, expected_trigger: str, timeout_ms: int = 120_000) -> bool:
        """
        Wait for 'Connect created!' message, return True iff the expected
        trigger app name is mentioned in it.
        """
        text = self.wait_for_connect_created(timeout_ms=timeout_ms)
        ok = expected_trigger.lower() in text.lower()
        if ok:
            logger.info("Trigger '%s' confirmed in copilot output.", expected_trigger)
        else:
            logger.warning(
                "Expected trigger '%s' NOT found in copilot output: %s",
                expected_trigger,
                text,
            )
        return ok
