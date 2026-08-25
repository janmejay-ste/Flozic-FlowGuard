"""flozic.ai entry-point connect creation: GoHighLevel.

PREVIOUSLY XFAIL — un-xfailed 2026-08-11.

  The old reason was: the build button routed to /register instead of
  /login, and the signup->login switch dropped the AIFormAutomationPrompt
  param, so the copilot never auto-built the connect.

  On the 2026-08-11 run this test XPASSed through the entire path: the
  build button went straight to /login (no signup detour), the editor
  opened at /customeditor, the copilot reported "Connect created!" with
  the right trigger and action, and the canvas AI verdict came back VALID.
  The marker was removed so a regression fails loudly instead of being
  absorbed as an expected failure.

  If this starts failing again, check FIRST whether the /register misroute
  has returned — that was the original cause and the signup->login switch
  in pages/signup_to_login_switch.py still handles it.
"""

from __future__ import annotations

from playwright.sync_api import Page

from tests._flozic_common import run_flozic_app_connect_test
from utils.test_category import test_category


@test_category(
    type="FULL",
    requires_login=True,
    feature="Flozic Entry Point",
)
class TestFlozicGoHighLevel:
    def test_flozic_gohighlevel_connect(self, page: Page) -> None:
        run_flozic_app_connect_test(page, "gohighlevel")
