"""
Shared driver for the 10 flozic.ai entry-point connect-creation tests.

Each per-app test file calls run_flozic_app_connect_test(page, app_key)
where app_key matches a top-level key in flozic_prompts.json.

Flow:
  1. Open https://www.flozic.ai/integrate/apps/{slug}/integrations
  2. Type the prompt from flozic_prompts.json into the .pb-inner textarea
  3. Click the 'Build my {App} workflow' button
  4. Perform login (delegates to existing perform_login helper)
  5. Wait for /customeditor URL
  6. Wait for copilot panel to open
  7. Assert 'Connect created!' message appears with the expected trigger

Per user spec: 'Just verify connect was created' — no Activate / Continue
walk-through after this point.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from playwright.sync_api import Page

from pages.auth_helper import perform_login
from pages.authv2_helper import handle_authv2_login_if_present
from pages.connect_canvas import screenshot_canvas, wait_for_canvas_populated
from pages.copilot_panel import CopilotPanel
from pages.flozic_landing_page import FlozicLandingPage
from pages.signup_to_login_switch import switch_signup_to_login_if_needed
from utils.ai_prompt_generator import generate_prompt, is_enabled as _ai_prompts_enabled
from utils.ai_validator import validate_canvas_with_ai
from utils.ai_visual_diff import diff_against_baseline

logger = logging.getLogger(__name__)

PROMPTS_FILE = Path(__file__).with_name("flozic_prompts.json")


def _load_prompts() -> dict:
    """Load and cache the prompts config from flozic_prompts.json."""
    with PROMPTS_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def run_flozic_app_connect_test(page: Page, app_key: str) -> None:
    """
    Execute the flozic.ai entry-point connect-creation flow for a single app.
    Verifies that the copilot 'Connect created!' message appears with the
    expected trigger app named.
    """
    prompts = _load_prompts()
    if app_key not in prompts:
        raise KeyError(
            f"App key '{app_key}' not found in {PROMPTS_FILE.name}. "
            f"Available keys: {[k for k in prompts if not k.startswith('_')]}"
        )
    cfg = prompts[app_key]
    slug = cfg["url_slug"]
    prompt = cfg["prompt"]
    expected_trigger = cfg["expected_trigger"]
    kind = cfg.get("kind", "app")  # "app" (default) | "agent"

    # ── Optional: GPT-generated prompt instead of the hardcoded JSON ────────
    # Activate by setting FLOZIC_AI_PROMPT=true (+ OPENAI_API_KEY). When the
    # generator returns a valid object, we use the AI prompt + AI's declared
    # trigger as the source of truth for this run. The generated prompt is
    # also persisted as an artifact so failures can be reproduced verbatim.
    generated = None
    if _ai_prompts_enabled():
        generated = generate_prompt(slug)
        if generated is not None:
            prompt = generated.prompt
            expected_trigger = generated.trigger_app
            logger.info(
                "[AI-prompt] Using generated prompt for slug=%s (trigger=%s)",
                slug, expected_trigger,
            )

    logger.info(
        "=== Starting flozic connect: app=%s, slug=%s, expected_trigger=%s ===",
        app_key, slug, expected_trigger,
    )

    landing = FlozicLandingPage(page)
    copilot = CopilotPanel(page)

    # Step 1-3: open the page (app or conversational-agent), type prompt, click build.
    if kind == "agent":
        landing.open_conversational_agent(slug)
    else:
        landing.open_app(slug)
    landing.submit_prompt(prompt)

    # Step 3.5: ISSUE handler — some apps (e.g. GoHighLevel) misroute the
    # build button to /register instead of /login. Detect this, log it as
    # an issue with the offending URL, then click the 'Sign in' link on
    # the signup page to switch to the login form before continuing.
    switch_signup_to_login_if_needed(page, app_key=app_key)

    # Step 4: login. The build-button click already navigated the browser to
    # accounts.appypie.com/login?...&AIFormAutomate=<base64-prompt>. We MUST
    # NOT re-issue page.goto(LOGIN_URL) — that would strip the AIFormAutomate
    # query param, and the server uses it to redirect to /customeditor after
    # auth. Also override the post-login URL pattern: we expect /customeditor,
    # not /connects.
    # After credentials submit, the OAuth flow may land on EITHER:
    #   (a) authv2.flozic.ai/login?...    — Cognito Hosted UI second-stage login
    #   (b) connectcloud.appypie.com/connects?AIFormAutomationPrompt=...
    # Accept both via a callable predicate (Playwright wait_for_url accepts a
    # str | regex | callable).
    def _accept_either(url: str) -> bool:
        # Accept legacy connectcloud.appypie.com AND new loop.flozic.ai —
        # partial migration window, both hosts in use.
        return (
            "authv2.flozic.ai" in url
            or "connectcloud.appypie.com/connects" in url
            or "connectcloud.appypie.com/customeditor" in url
            or "loop.flozic.ai/connects" in url
            or "loop.flozic.ai/customeditor" in url
        )

    perform_login(
        page,
        auto_timeout_ms=30_000,
        manual_fallback_minutes=3,
        skip_initial_navigation=True,
        post_login_url_pattern=_accept_either,
    )

    # Step 4.5: TEMPORARY — if the OAuth flow redirected to the authv2.flozic.ai
    # Cognito Hosted UI for a second-stage login, complete it. Returns False if
    # the redirect didn't happen (normal path), True if credentials were resubmitted.
    handle_authv2_login_if_present(page)

    # Step 5: wait for the final destination. Branch by flow kind:
    #   - App flow:   /customeditor — workflow auto-builds, copilot reports back
    #   - Agent flow: /agent/builder?agent=chat — conversational bot builder UI
    #
    # PRODUCT BUG: agent flows currently misroute to /connects (the workflow
    # dashboard) instead of /agent/builder. wait_for_agent_builder() logs an
    # [ISSUE] line when this happens and re-raises so the test fails loudly.
    if kind == "agent":
        landing.wait_for_agent_builder(timeout_ms=60_000)
        # Agent flow has a different success contract than apps — no copilot
        # panel + 'Connect created!' message. For now, reaching the builder
        # URL IS the success gate. Skip the app-specific copilot assertions.
        logger.info(
            "=== SUCCESS: flozic agent builder reached for agent=%s ===", app_key,
        )
        return

    landing.wait_for_customeditor(timeout_ms=120_000)

    # Step 6-7: copilot opens and reports 'Connect created!'.
    # gohighlevel is xfail'd (product bug — signup-redirect drops the prompt
    # param) and uses shorter timeouts so the suite fails fast on it instead
    # of stalling 60s + 180s waiting for events that never come.
    FAST_FAIL_APPS = {"gohighlevel"}
    if app_key in FAST_FAIL_APPS:
        panel_timeout = 15_000
        trigger_timeout = 30_000
    else:
        panel_timeout = 60_000
        trigger_timeout = 180_000

    copilot.wait_for_panel_open(timeout_ms=panel_timeout)
    confirmed = copilot.verify_trigger(expected_trigger, timeout_ms=trigger_timeout)
    assert confirmed, (
        f"Copilot did not report expected trigger '{expected_trigger}' "
        f"for app '{app_key}'."
    )

    # ── Step 8: wait for the CANVAS to actually populate ─────────────────────
    # The copilot announces "Connect created!" several seconds before the
    # right-hand canvas cards swap their "Select Trigger App" placeholders
    # for the real app names. Logging out before the swap captures a
    # half-built workflow in the recording. Poll up to 90s for the canvas
    # to settle (fast-exit on success).
    canvas_ready = wait_for_canvas_populated(
        page, expected_trigger,
        total_timeout_ms=90_000, poll_interval_ms=5_000,
    )

    # ── Step 9: screenshot the canvas, regardless of populated/not ───────────
    # Even a half-built canvas is useful evidence for AI/human review.
    test_id = f"test_flozic_{app_key.replace('-', '_')}_connect"
    artifact_dir = Path("reports/recordings") / test_id
    canvas_png = artifact_dir / "canvas.png"
    screenshot_canvas(page, canvas_png)

    # Persist the AI-generated prompt (if any) so the run is reproducible.
    if generated is not None:
        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "generated_prompt.json").write_text(
                generated.to_json(), encoding="utf-8"
            )
        except Exception as e:
            logger.warning("Could not persist generated prompt: %s", e)

    # ── Step 9.5: visual regression vs the baseline canvas ─────────────────
    # FIRST_RUN  -> caller will promote later via scripts/promote_baseline.py
    # IDENTICAL  -> byte-equal; no GPT cost
    # COSMETIC   -> render-noise; non-fatal
    # REGRESSION -> something moved/changed/disappeared -> FAIL the test
    # UNKNOWN    -> GPT couldn't tell; non-fatal but visible on dashboard
    visual = diff_against_baseline(
        test_id=test_id,
        current_png=canvas_png,
        output_verdict_path=artifact_dir / "visual_diff.json",
    )
    if visual.status == "REGRESSION":
        raise AssertionError(
            f"Visual regression for {test_id}: {visual.reasoning} "
            f"(see {artifact_dir / 'visual_diff.json'})"
        )

    # ── Step 10: AI validation (optional — requires OPENAI_API_KEY) ──────────
    # Behaviour matrix:
    #   no API key     -> SKIPPED  -> pass on text copilot msg only
    #   API key + VALID    -> pass
    #   API key + INVALID  -> FAIL  (the AI says the canvas is wrong)
    #   API key + ERROR    -> log and fall back to text msg (don't penalise
    #                         tests for an OpenAI outage)
    verdict_path = artifact_dir / "ai_verdict.json"
    ai = validate_canvas_with_ai(
        screenshot_path=canvas_png,
        user_prompt=prompt,
        expected_trigger=expected_trigger,
        verdict_output_path=verdict_path,
    )

    if ai.status == "VALID":
        logger.info("[AI] Canvas confirmed VALID — %s", ai.reasoning)
    elif ai.status == "INVALID":
        # Hard fail — the AI saw the canvas and rejected it.
        raise AssertionError(
            f"AI rejected the canvas for app '{app_key}'. "
            f"Reason: {ai.reasoning}  "
            f"(see {verdict_path} and {canvas_png})"
        )
    elif ai.status == "SKIPPED":
        # No API key — already passed the copilot text check above, so
        # we accept that as the gate.
        logger.info(
            "[AI] Skipped (no OPENAI_API_KEY). Falling back to text copilot "
            "message as the success gate. canvas_ready=%s",
            canvas_ready,
        )
    else:  # ERROR
        logger.warning(
            "[AI] Validation errored (%s); falling back to text copilot "
            "message. canvas_ready=%s",
            ai.error or ai.reasoning, canvas_ready,
        )

    logger.info("=== SUCCESS: flozic connect verified for app=%s ===", app_key)
