"""
Parametrized flozic connect-creation: N GPT-generated prompt variants per app.

Each parameter set is one (app_slug, variant_index) pair. The shared driver
generates N varied prompts up-front via ai_prompt_generator.generate_prompts(),
then this test runs each one.

Activate by setting OPENAI_API_KEY. Tune count via:
    FLOZIC_VARIANTS_PER_APP=3    # default 3

Skip apps that don't have an integrations page via FLOZIC_DIVERSE_APPS
(comma-separated slug list). Default uses the same 10 as the hardcoded tests.

Cost note: each test makes 1 generator call (shared across variants for the
same app) + 1 vision-validator call. With 3 variants × 10 apps = 30 runs,
expect ~$0.50-$1.50 per full suite at gpt-4o pricing.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest
from playwright.sync_api import Page

from pages.auth_helper import perform_login
from pages.authv2_helper import handle_authv2_login_if_present
from pages.connect_canvas import screenshot_canvas, wait_for_canvas_populated
from pages.copilot_panel import CopilotPanel
from pages.flozic_landing_page import FlozicLandingPage
from pages.signup_to_login_switch import switch_signup_to_login_if_needed
from pages.auth_state import is_on_auth_host
from utils.ai_prompt_generator import generate_prompts
from utils.ai_validator import validate_canvas_with_ai
from utils.test_category import test_category

logger = logging.getLogger(__name__)


def _default_apps() -> list[str]:
    return [
        "google-sheets", "cliniko", "chatgpt", "housecall-pro",
        "microsoft-excel", "telegram", "mindbody", "gmail", "paypal",
        # gohighlevel intentionally omitted — known signup-routing product bug.
    ]


def _variant_count() -> int:
    try:
        return max(1, min(10, int(os.environ.get("FLOZIC_VARIANTS_PER_APP", "3"))))
    except ValueError:
        return 3


def _apps_under_test() -> list[str]:
    env = os.environ.get("FLOZIC_DIVERSE_APPS", "").strip()
    if env:
        return [s.strip() for s in env.split(",") if s.strip()]
    return _default_apps()


# Generate all variants UP FRONT at module import so the parametrize() ids
# are stable. If OPENAI_API_KEY is missing, this yields an empty list and
# the test collects as 0 items — pytest will report "no tests ran".
def _all_variants() -> list[tuple[str, int, dict]]:
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        logger.warning(
            "[diverse] OPENAI_API_KEY not set — diversified tests will not "
            "be parametrized. Set the key to enable."
        )
        return []
    n = _variant_count()
    out: list[tuple[str, int, dict]] = []
    for slug in _apps_under_test():
        variants = generate_prompts(slug, n=n)
        if not variants:
            logger.warning("[diverse] No variants for slug=%s; skipping.", slug)
            continue
        for i, v in enumerate(variants):
            out.append((slug, i, {
                "prompt":       v.prompt,
                "trigger_app":  v.trigger_app,
                "action_apps":  v.action_apps,
                "reasoning":    v.reasoning,
            }))
    return out


VARIANTS = _all_variants()


@test_category(
    type="FULL",
    requires_login=True,
    feature="Flozic Entry Point (Diversified)",
)
class TestFlozicDiversified:
    @pytest.mark.parametrize(
        ("slug", "variant_index", "variant"),
        VARIANTS,
        ids=[f"{s}-v{i}" for s, i, _ in VARIANTS],
    )
    def test_flozic_diversified_connect(
        self, page: Page, slug: str, variant_index: int, variant: dict
    ) -> None:
        prompt           = variant["prompt"]
        expected_trigger = variant["trigger_app"]
        logger.info(
            "=== Diversified flozic: slug=%s variant=%d trigger=%s prompt=%r ===",
            slug, variant_index, expected_trigger, prompt,
        )

        landing = FlozicLandingPage(page)
        copilot = CopilotPanel(page)

        landing.open_app(slug)
        landing.submit_prompt(prompt)
        switch_signup_to_login_if_needed(page, app_key=slug)

        def _accept_either(url: str) -> bool:
            return (
                # Any Cognito host — see pages/auth_state.AUTH_HOSTS
                is_on_auth_host(url)
                or "connectcloud.appypie.com/connects" in url
                or "connectcloud.appypie.com/customeditor" in url
            )

        perform_login(
            page,
            auto_timeout_ms=30_000,
            manual_fallback_minutes=3,
            skip_initial_navigation=True,
            post_login_url_pattern=_accept_either,
        )
        handle_authv2_login_if_present(page)
        landing.wait_for_customeditor(timeout_ms=120_000)

        copilot.wait_for_panel_open(timeout_ms=60_000)
        assert copilot.verify_trigger(expected_trigger, timeout_ms=180_000), (
            f"Copilot did not report expected trigger '{expected_trigger}'"
        )

        wait_for_canvas_populated(
            page, expected_trigger,
            total_timeout_ms=90_000, poll_interval_ms=5_000,
        )
        artifact_dir = Path("reports/recordings") / (
            f"test_flozic_diversified_{slug.replace('-', '_')}_v{variant_index}"
        )
        canvas_png = artifact_dir / "canvas.png"
        screenshot_canvas(page, canvas_png)

        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "generated_prompt.json").write_text(
                json.dumps(variant, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

        ai = validate_canvas_with_ai(
            screenshot_path=canvas_png,
            user_prompt=prompt,
            expected_trigger=expected_trigger,
            verdict_output_path=artifact_dir / "ai_verdict.json",
        )
        if ai.status == "INVALID":
            raise AssertionError(
                f"AI rejected canvas for {slug} v{variant_index}: {ai.reasoning}"
            )
