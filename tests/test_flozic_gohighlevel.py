"""flozic.ai entry-point connect creation: GoHighLevel.

KNOWN PRODUCT ISSUE (expected failure):
  The flozic.ai 'Build my GoHighLevel workflow' button routes to
  /register (signup) instead of /login. Our test auto-clicks the
  'Login' link to switch, but the switch URL drops the
  AIFormAutomationPrompt query param, so the customeditor opens
  without the prompt and the copilot never auto-builds the connect.

  Until the product team fixes the routing (or preserves the prompt
  param through the signup->login switch), this test is xfail.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

from tests._flozic_common import run_flozic_app_connect_test
from utils.test_category import test_category


@test_category(
    type="FULL",
    requires_login=True,
    feature="Flozic Entry Point",
)
@pytest.mark.xfail(
    reason=(
        "Product bug: flozic.ai gohighlevel build button routes to "
        "/register; signup->login switch drops AIFormAutomationPrompt; "
        "copilot never auto-builds. See logs for ISSUE warning + URL."
    ),
    strict=False,  # XPASS allowed — if it ever starts passing, we want to know.
)
class TestFlozicGoHighLevel:
    def test_flozic_gohighlevel_connect(self, page: Page) -> None:
        run_flozic_app_connect_test(page, "gohighlevel")
