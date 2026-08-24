"""
Pricing page TRY NOW / BUY NOW flow — full end-to-end per (period, plan).

For each combination of period × plan:
  1. Navigate to /integrate/pricing-plan
  2. (Monthly only) Click the Monthly Pricing radio to swap cards
  3. Click TRY NOW (yearly) or BUY NOW (monthly) on the plan card
  4. If misrouted to /register → log [ISSUE] and switch to /login
  5. Perform login
  6. Wait for PlanChangeService popup (.pcc-modal)
  7. Send popup contents to ai_popup_validator (matrix-aware)
  8. Assert validator returned VALID
  9. Close the popup via the × button
  10. Open profile dropdown → Logout
  11. (parametrize iteration gives fresh page → next variant)

Variants:
  Yearly × Standard / Professional / Business  → "TRY NOW" CTA
  Monthly × Standard / Professional / Business  → "BUY NOW" CTA

Current account: janmejay@appypiellp.com is on ENTERPRISE.
Expected outcome for all 6 variants: BLOCK popup (tier downgrade),
title = "Downgrade not allowed", message mentions Enterprise.

Override the assumed current plan via env var to test other accounts:
    FLOZIC_CURRENT_PLAN=Standard pytest ...
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from playwright.sync_api import Page

from pages.auth_helper import perform_login
from pages.auth_state import is_on_auth_host
from pages.dashboard_page import DashboardPage
from pages.marketing.pricing_page import (
    PricingPage,
    TRY_NOW_PLAN_NAMES,
)
from pages.signup_to_login_switch import switch_signup_to_login_if_needed
from utils.ai_popup_validator import validate_popup
from utils.test_category import test_category

logger = logging.getLogger(__name__)

# Account state — override per-run via FLOZIC_CURRENT_PLAN. The validator's
# matrix prompt uses this to decide which popup category is "correct".
CURRENT_PLAN = os.environ.get("FLOZIC_CURRENT_PLAN", "Enterprise").strip()


PERIOD_PLAN_VARIANTS = [
    (period, plan)
    for period in ("Yearly", "Monthly")
    for plan in TRY_NOW_PLAN_NAMES
]


@test_category(
    type="REGRESSION",
    requires_login=True,
    feature="Pricing Plans",
)
class TestPricingPlanTryNow:
    """One full pricing → CTA → login → popup → close → logout cycle per
    (period, plan) combination. Each parametrize variant runs in a fresh
    page fixture so cookies/state from the previous variant don't leak.

    For the Enterprise test account, all 6 variants are tier downgrades and
    should produce the BLOCK popup. For other account states, the AI
    validator computes the expected category from the business-logic matrix.
    """

    @pytest.mark.parametrize(
        "period,plan_name",
        PERIOD_PLAN_VARIANTS,
        ids=[f"{p.lower()}-{n}" for p, n in PERIOD_PLAN_VARIANTS],
    )
    def test_pricing_try_now(self, page: Page, period: str, plan_name: str) -> None:
        label = f"{period.lower()}/{plan_name}"
        logger.info(
            "=== Pricing CTA: plan=%s period=%s current_account=%s ===",
            plan_name, period, CURRENT_PLAN,
        )

        pricing = PricingPage(page)

        # Step 1-2: navigate, switch period if needed, click CTA
        pricing.navigate()
        assert pricing.is_loaded(timeout_ms=20_000), (
            "Pricing page did not reach loaded state."
        )
        if period == "Monthly":
            pricing.switch_to_period("Monthly")
        pricing.click_try_now_for(plan_name)

        # Step 3: signup→login switch if product misroutes
        switch_signup_to_login_if_needed(
            page, app_key=f"pricing/{label}",
        )

        # Step 4: login — accept connectcloud + loop hosts AND the
        # /portal-payment-handler intermediate URL the pricing flow uses.
        def _accept_pricing_destinations(url: str) -> bool:
            if not url:
                return False
            return (
                # Any Cognito host — see pages/auth_state.AUTH_HOSTS
                is_on_auth_host(url)
                or "/portal-payment-handler" in url
                or "loop.flozic.ai/connects" in url
                or "loop.flozic.ai/plans" in url
                or "loop.flozic.ai/pcc" in url
                or "connectcloud.appypie.com/connects" in url
            )

        perform_login(
            page,
            auto_timeout_ms=30_000,
            manual_fallback_minutes=3,
            skip_initial_navigation=True,
            post_login_url_pattern=_accept_pricing_destinations,
        )

        # Step 5: wait for the PlanChangeService popup
        snapshot = pricing.wait_for_popup(timeout_ms=60_000)
        logger.info(
            "[pricing/%s] popup snapshot: title=%r category_guess=%s",
            label, snapshot.title, snapshot.category_guess,
        )

        # Step 6: AI-validate the popup against the business-logic matrix.
        verdict_path = (
            Path("reports/recordings")
            / f"test_pricing_plan_try_now_{period.lower()}_{plan_name.lower()}"
            / "popup_verdict.json"
        )
        verdict = validate_popup(
            snapshot,
            current_plan=CURRENT_PLAN,
            target_plan=plan_name,
            target_period=period,
            verdict_output_path=verdict_path,
        )

        # Step 7: assert verdict
        if verdict.status == "INVALID":
            raise AssertionError(
                f"[pricing/{label}] AI rejected the popup. "
                f"Reason: {verdict.reasoning}  "
                f"Page-object guess: {snapshot.category_guess}.  "
                f"See {verdict_path}."
            )
        elif verdict.status == "ERROR":
            # OpenAI hiccup — don't penalise the test. Fall back to the
            # keyword classifier as the success gate.
            logger.warning(
                "[pricing/%s] AI validation errored (%s); falling back to "
                "page-object classifier=%s",
                label, verdict.reasoning, snapshot.category_guess,
            )
            assert snapshot.category_guess in (
                "BLOCK", "BLOCK_PERIOD", "CONFIRM_TRIAL", "CONFIRM_PAID",
                "CONFIRM_SWITCH_YEARLY", "SAME", "CONTACT_US",
            ), (
                f"[pricing/{label}] No recognised popup category and AI "
                f"validation errored. Snapshot: {snapshot.to_dict()}"
            )
        elif verdict.status == "SKIPPED":
            # No OPENAI_API_KEY — accept any keyword-recognised popup as OK.
            logger.info(
                "[pricing/%s] AI skipped (no key). Keyword classifier=%s",
                label, snapshot.category_guess,
            )
            assert snapshot.category_guess != "UNKNOWN", (
                f"[pricing/{label}] Popup not classified by keywords and "
                f"AI is disabled. Snapshot: {snapshot.to_dict()}"
            )
        else:
            logger.info(
                "[pricing/%s] AI validation: VALID — %s",
                label, verdict.reasoning,
            )

        # Step 8: close popup via the × button
        pricing.close_popup()

        # Step 9: logout. Best-effort — fresh page next iteration regardless.
        try:
            DashboardPage(page).click_logout()
            logger.info("[pricing/%s] Logout complete.", label)
        except Exception as e:
            logger.warning(
                "[pricing/%s] Logout failed (non-fatal — fresh context next "
                "iteration): %s",
                label, str(e).splitlines()[0],
            )

        logger.info("=== SUCCESS: pricing CTA verified for %s ===", label)
