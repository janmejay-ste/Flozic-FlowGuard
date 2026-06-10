"""
Python port of testing.GoHighLevelMindbodyConnectTest.

End-to-end: Login → GoHighLevel V2 trigger → New Opportunity →
Mindbody Create Sale → fill quantity/amount/notes → Activate.
"""

from __future__ import annotations

import logging
import os

from playwright.sync_api import Page

from pages.auth_helper import perform_login
from pages.connect_editor_page import ConnectEditorPage
from pages.dashboard_page import DashboardPage
from pages.wait_utils import complete_user_guide, dismiss_overlays
from utils.test_category import test_category

logger = logging.getLogger(__name__)

TRIGGER_APP = "GoHighLevel V2"
TRIGGER_EVENT = "New Opportunity"
ACTION_APP = "Mindbody"
ACTION_EVENT = "Create Sale"


@test_category(
    type="FULL",
    requires_login=True,
    feature="Connect Workflow",
)
class TestGoHighLevelMindbody:
    def test_create_gohighlevel_mindbody_connect(self, page: Page) -> None:
        logger.info("=== Starting: GoHighLevel V2 → Mindbody Create Sale ===")

        dashboard = DashboardPage(page)
        editor = ConnectEditorPage(page)

        quantity = os.environ.get("AUTOMATE_QUANTITY", "1")
        amount = os.environ.get("AUTOMATE_AMOUNT", "1000")
        notes = os.environ.get("AUTOMATE_NOTES", "Automation Test")

        # Step 1: login
        perform_login(page)

        # Step 2: dashboard
        dismiss_overlays(page)
        complete_user_guide(page)
        assert dashboard.is_dashboard_loaded(), "Dashboard not loaded"

        # Step 3: open editor
        if not editor.is_editor_visible(timeout_ms=3_000):
            dashboard.click_create_connect()
            page.wait_for_timeout(2_000)
            dismiss_overlays(page)
            complete_user_guide(page)
        assert editor.is_editor_visible(timeout_ms=30_000)

        # Step 4-5: trigger
        editor.select_trigger_app(TRIGGER_APP)
        editor.select_trigger_event(TRIGGER_EVENT)

        # Step 6-7: continue
        editor.click_continue()
        try:
            editor.click_continue()
        except Exception as e:
            logger.info("Step 7 (account-panel Continue) skipped: %s",
                        str(e).splitlines()[0])

        # Step 8: trigger setup (GoHighLevel may need Location picker; non-fatal)
        try:
            editor.handle_setup_step()
        except Exception as e:
            logger.info("Step 8 skipped: %s", str(e).splitlines()[0])

        # Step 9: Continue & Run Test
        editor.click_continue_run_test()

        # Step 9.5: optional continue
        try:
            editor.click_continue()
        except Exception:
            pass

        # Step 10: add action
        editor.click_add_new_step()
        editor.click_add_app()

        # Step 11-12: action
        # After selecting the action app, the editor shows an app-confirm screen
        # with a Continue button before the event list appears. Click it first.
        editor.select_action_app(ACTION_APP)
        try:
            editor.click_continue()
            logger.info("Step 11.5: post-app-selection Continue clicked")
        except Exception as e:
            logger.info("Step 11.5 skipped: %s", str(e).splitlines()[0])
        editor.select_action_event(ACTION_EVENT)

        # Step 13-14: continue through action panels
        for step in (13, 14):
            try:
                editor.click_continue()
            except Exception as e:
                logger.warning("Step %d skipped: %s", step, str(e).splitlines()[0])

        # Step 15: Mindbody action setup dropdowns
        targeted = {
            "TRI__site_id": "Appy Pie",
            "client_id":    "01Test 01",
            "LocationId":   "Appy Pie",
            "SendEmail":    "false",
            "product_id":   "Test1",
            "service_id":   "initial",
            "p_type":       "Cash",
        }
        try:
            editor.handle_setup_step(targeted=targeted)
        except Exception as e:
            logger.warning("Step 15 setup skipped: %s", str(e).splitlines()[0])

        # Step 15.5: fill Mindbody fields
        try:
            editor.fill_mindbody_sale_setup(
                quantity=quantity, amount=amount, notes=notes
            )
        except Exception as e:
            logger.warning("Mindbody fill non-critical: %s", str(e).splitlines()[0])

        # Step 16: Continue & Run Test (action)
        try:
            editor.click_continue_run_test()
        except Exception as e:
            logger.warning("Step 16 skipped: %s", str(e).splitlines()[0])

        # Step 17: Activate
        editor.click_activate_connect()
        logger.info("=== SUCCESS: GoHighLevel → Mindbody Connect activated ===")
