"""flozic.ai conversational-agent entry point: WhatsApp bot."""

from __future__ import annotations

from playwright.sync_api import Page

from tests._flozic_common import run_flozic_app_connect_test
from utils.test_category import test_category


@test_category(
    type="FULL",
    requires_login=True,
    feature="Flozic Conversational Agent",
)
class TestFlozicAgentWhatsAppBot:
    def test_flozic_agent_whatsapp_bot(self, page: Page) -> None:
        run_flozic_app_connect_test(page, "whatsapp-bot")
