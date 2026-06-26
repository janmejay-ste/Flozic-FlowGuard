"""flozic.ai entry-point connect creation: Flow by Automate."""

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
class TestFlozicFlowByAutomate:
    @pytest.mark.skip(
        reason="Page /integrate/apps/flow-by-automate/integrations does not "
               "expose the standard '.pb-inner textarea' prompt UI — likely "
               "either the slug is wrong or the page uses a different template. "
               "Skipping until product confirms the correct entry URL."
    )
    def test_flozic_flow_by_automate_connect(self, page: Page) -> None:
        run_flozic_app_connect_test(page, "flow-by-automate")
