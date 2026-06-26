"""flozic.ai conversational-agent entry point: WhatsApp bot."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

from tests._flozic_common import run_flozic_app_connect_test
from utils.test_category import test_category

AGENT_MISROUTE_REASON = (
    "Product bug: conversational-agent build button misroutes the OAuth flow "
    "to /connects (workflow dashboard) instead of /agent/builder?agent=chat. "
    "Agent builder UI never appears. See [ISSUE] warning in logs."
)


@test_category(
    type="FULL",
    requires_login=True,
    feature="Flozic Conversational Agent",
)
class TestFlozicAgentWhatsAppBot:
    @pytest.mark.xfail(reason=AGENT_MISROUTE_REASON, strict=False)
    def test_flozic_agent_whatsapp_bot(self, page: Page) -> None:
        run_flozic_app_connect_test(page, "whatsapp-bot")
