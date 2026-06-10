"""
Python port of testing.CreateConnectWorkflowTest.

End-to-end: Login → Dashboard → Create Connect → select Google Sheets
trigger → New Spreadsheet Row → setup → add Gmail action → Create
Draft → Activate.

This is a 17-step workflow against the live Connect editor.
Requires headed mode (auth flow + R-1 captcha gate).
"""

from __future__ import annotations

import logging

from playwright.sync_api import Page

from pages.auth_helper import perform_login
from pages.connect_editor_page import ConnectEditorPage
from pages.dashboard_page import DashboardPage
from pages.wait_utils import complete_user_guide, dismiss_overlays
from utils.test_category import test_category

logger = logging.getLogger(__name__)

TRIGGER_APP = "Google Sheets"
TRIGGER_EVENT = "New Spreadsheet Row"
ACTION_APP = "Gmail"
ACTION_EVENT = "Create Draft"


@test_category(
    type="FULL",
    requires_login=True,
    feature="Connect Workflow",
)
class TestCreateConnectWorkflow:
    def test_create_app_sheet_email_connect(self, page: Page) -> None:
        logger.info("=== Starting: Google Sheets → Gmail Draft Workflow ===")

        dashboard = DashboardPage(page)
        editor = ConnectEditorPage(page)

        # Step 1: login
        perform_login(page)
        logger.info("Step 1 DONE: Logged in.")

        # Step 2: dashboard
        dismiss_overlays(page)
        complete_user_guide(page)
        assert dashboard.is_dashboard_loaded(), "Dashboard did not load"
        logger.info("Step 2 DONE: Dashboard loaded.")

        # Step 3: open editor
        if not editor.is_editor_visible(timeout_ms=3_000):
            dashboard.click_create_connect()
            page.wait_for_timeout(2_000)
            dismiss_overlays(page)
            complete_user_guide(page)
        assert editor.is_editor_visible(timeout_ms=30_000), "Editor not visible"
        logger.info("Step 3 DONE: Editor visible.")

        # Step 4-5: trigger app + event
        editor.select_trigger_app(TRIGGER_APP)
        editor.select_trigger_event(TRIGGER_EVENT)

        # Step 6-7: continue through event + account panels
        editor.click_continue()
        try:
            editor.click_continue()
        except Exception as e:
            logger.info("Step 7 skipped: %s", str(e).splitlines()[0])

        # Step 8: setup dropdowns (Spreadsheet + Worksheet)
        try:
            editor.handle_setup_step()
        except Exception as e:
            logger.info("Step 8 skipped: %s", str(e).splitlines()[0])

        # Step 9: Continue & Run Test
        editor.click_continue_run_test()

        # Step 9.5: optional post-run continue
        try:
            editor.click_continue()
        except Exception:
            pass

        # Step 10: add action app
        editor.click_add_new_step()
        editor.click_add_app()

        # Step 11-12: action app + event
        # After selecting the action app an app-confirm screen appears with a
        # Continue button before the event list is shown. Click it first.
        editor.select_action_app(ACTION_APP)
        try:
            editor.click_continue()
            logger.info("Step 11.5: post-app-selection Continue clicked")
        except Exception as e:
            logger.info("Step 11.5 skipped: %s", str(e).splitlines()[0])
        editor.select_action_event(ACTION_EVENT)

        # Step 13-14: continue through action event + account
        for step in (13, 14):
            try:
                editor.click_continue()
            except Exception as e:
                logger.warning("Step %d skipped: %s", step, str(e).splitlines()[0])

        # Step 15: action setup
        try:
            editor.handle_setup_step()
        except Exception as e:
            logger.info("Step 15 skipped: %s", str(e).splitlines()[0])

        # Step 15.5: fill Gmail draft
        try:
            editor.fill_gmail_draft_setup()
        except Exception as e:
            logger.warning("Gmail Draft fill non-critical: %s", str(e).splitlines()[0])

        # Step 16: Continue & Run Test (action)
        try:
            editor.click_continue_run_test()
        except Exception as e:
            logger.warning("Step 16 skipped: %s", str(e).splitlines()[0])

        # Step 17: Activate
        editor.click_activate_connect()
        logger.info("=== SUCCESS: Connect created and activated ===")
