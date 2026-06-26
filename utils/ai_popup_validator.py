"""
AI-assisted validation of the PlanChangeService popup that appears after
clicking TRY NOW on the marketing pricing page.

The page-object's keyword classifier (pages.marketing.pricing_page._classify_popup)
makes a fast first guess. This validator is the second opinion: given the
captured popup + the account's current plan + the user's target, it judges
whether the popup category matches the business-logic matrix.

Returns the same AIValidationResult shape as utils.ai_validator so the
dashboard can render popup verdicts identically to canvas verdicts.

JSON contract (enforced via response_format=json_object):
  {
    "is_valid":            true|false,
    "expected_category":   "BLOCK" | "CONFIRM_TRIAL" | "CONFIRM_PAID" | ...,
    "observed_category":   "BLOCK" | ...,
    "categories_match":    true|false,
    "popup_mentions_target": true|false,
    "popup_mentions_current": true|false,
    "reasoning":           "<one sentence>"
  }
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
API_BASE = "https://api.openai.com/v1/chat/completions"
REQUEST_TIMEOUT_S = 30


@dataclass
class AIValidationResult:
    """Outcome of popup validation. Same shape as utils.ai_validator."""
    status: str             # "VALID" | "INVALID" | "SKIPPED" | "ERROR"
    is_valid: bool
    reasoning: str
    raw: dict = field(default_factory=dict)
    error: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


# Full PlanChangeService business-logic matrix as model context. The matrix
# is the contract — if the model needs to know *why* BLOCK is correct for
# a Standard click from an Enterprise account, this paragraph tells it.
MATRIX_CONTEXT = """\
Flozic plan tier order (lower → higher):
    Standard (1) < Professional (2) < Business (3) < Enterprise (4)

When a user clicks TRY NOW on the marketing pricing page, they're routed to
/portal-payment-handler/<planId>/<cpId>/<period> which invokes PlanChangeService.
The service decides which popup to show based on the user's current plan vs
the target plan + period. The full decision matrix:

  CURRENT = Free (new user):
    target = Standard / Professional / Business → NO POPUP (proceeds to checkout)
    target = Enterprise → CONTACT_US (opens Calendly)

  CURRENT = Trial of any tier:
    target = SAME tier → SAME ("already your current plan")
    target = HIGHER tier → CONFIRM_TRIAL
        ("You are currently on the X trial plan. Upgrading to / Purchasing the
          selected plan will immediately end your trial and activate the new
          plan. Do you want to continue?")
    target = LOWER tier (yearly section) → BLOCK
        ("Downgrading to a lower plan is not allowed while your X trial is
          active. Please continue using your current plan or contact Support
          for assistance.")
    target = LOWER tier (monthly section) → CONFIRM_PAID (special case — allowed)
    target = Enterprise → CONTACT_US

  CURRENT = Paid (any tier, yearly or monthly):
    target = SAME tier + SAME period → SAME
    target = SAME tier, monthly→yearly → CONFIRM_SWITCH_YEARLY
    target = SAME tier, yearly→monthly → BLOCK_PERIOD
        ("Switching from a yearly to a monthly plan is not allowed…")
    target = HIGHER tier → CONFIRM_PAID
        ("You are currently subscribed to the X period plan. Purchasing the
          selected plan will cancel your current plan and activate the new
          plan immediately…")
    target = LOWER tier → BLOCK
    target = Enterprise → CONTACT_US

Test-account state: janmejay@appypiellp.com is on ENTERPRISE (highest tier).
Therefore every Standard / Professional / Business click is a DOWNGRADE and
should produce BLOCK with .pcc-title = "Downgrade not allowed" and a message
that says the Enterprise plan is active.
"""


def _build_prompt(
    popup_title:        str,
    popup_message:      str,
    popup_primary:      str,
    popup_secondary:    str,
    page_classifier:    str,
    current_plan:       str,
    target_plan:        str,
    target_period:      str,
) -> str:
    return (
        "You are validating a PlanChangeService popup from the flozic.ai "
        "pricing flow.\n\n"
        + MATRIX_CONTEXT
        + "\n\nObservation from this test run:\n"
        f"  Current plan:           {current_plan}\n"
        f"  Target plan (clicked):  {target_plan}\n"
        f"  Target period:          {target_period}\n"
        f"  Page-object classifier guess:  {page_classifier}\n"
        "\nPopup as captured from the DOM:\n"
        f"  .pcc-title:           {popup_title!r}\n"
        f"  .pcc-message:         {popup_message!r}\n"
        f"  primary action button:    {popup_primary!r}\n"
        f"  secondary action button:  {popup_secondary!r}\n"
        "\nDecide:\n"
        "  1. Given the matrix and the current account state, what category "
        "SHOULD the popup be? (BLOCK / CONFIRM_TRIAL / CONFIRM_PAID / "
        "CONFIRM_SWITCH_YEARLY / BLOCK_PERIOD / SAME / NO_POPUP / CONTACT_US)\n"
        "  2. Given the captured text, what category did the product ACTUALLY "
        "show?\n"
        "  3. Do they match?\n"
        "  4. Does the message correctly mention the user's current plan "
        "(needed for BLOCK / CONFIRM_PAID / CONFIRM_TRIAL)?\n"
        "  5. Does the message correctly mention the target plan?\n"
        "\nReply with a SINGLE JSON object, no other text:\n"
        "  is_valid (bool — true iff categories match AND plan names "
        "correctly mentioned),\n"
        "  expected_category (string),\n"
        "  observed_category (string),\n"
        "  categories_match (bool),\n"
        "  popup_mentions_target (bool),\n"
        "  popup_mentions_current (bool),\n"
        "  reasoning (string, one sentence)."
    )


def validate_popup(
    snapshot,                       # PopupSnapshot from pricing_page
    current_plan:       str,
    target_plan:        str,
    target_period:      str = "Yearly",
    model:              str | None = None,
    verdict_output_path: Path | None = None,
) -> AIValidationResult:
    """
    Send the captured popup to OpenAI for matrix-aware validation.

    Returns SKIPPED when OPENAI_API_KEY is missing — caller should fall back
    to the page-object classifier alone in that case.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        result = AIValidationResult(
            status="SKIPPED", is_valid=False,
            reasoning="OPENAI_API_KEY not set — popup validated by keyword "
                      "classifier only.",
        )
        return _persist(result, verdict_output_path)

    try:
        import requests
    except ImportError:
        return _persist(AIValidationResult(
            status="ERROR", is_valid=False,
            reasoning="requests library not installed.",
            error="ImportError: requests",
        ), verdict_output_path)

    model = model or DEFAULT_MODEL
    prompt = _build_prompt(
        popup_title=snapshot.title,
        popup_message=snapshot.message,
        popup_primary=snapshot.primary_action,
        popup_secondary=snapshot.secondary_action,
        page_classifier=snapshot.category_guess,
        current_plan=current_plan,
        target_plan=target_plan,
        target_period=target_period,
    )
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }

    try:
        resp = requests.post(
            API_BASE, json=payload, headers=headers, timeout=REQUEST_TIMEOUT_S
        )
    except Exception as e:
        logger.error("[AI-popup] OpenAI request failed: %s", e)
        return _persist(AIValidationResult(
            status="ERROR", is_valid=False,
            reasoning=f"OpenAI request failed: {e}", error=str(e),
        ), verdict_output_path)

    if resp.status_code != 200:
        body = resp.text[:500]
        logger.error("[AI-popup] OpenAI %d: %s", resp.status_code, body)
        return _persist(AIValidationResult(
            status="ERROR", is_valid=False,
            reasoning=f"OpenAI HTTP {resp.status_code}", error=body,
        ), verdict_output_path)

    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        if content is None or not isinstance(content, str):
            raise ValueError(
                f"OpenAI returned empty/non-string content (type={type(content).__name__})"
            )
        parsed: dict[str, Any] = json.loads(content)
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as e:
        logger.error("[AI-popup] Could not parse OpenAI response: %s", e)
        return _persist(AIValidationResult(
            status="ERROR", is_valid=False,
            reasoning=f"Could not parse model output: {e}", error=str(e),
        ), verdict_output_path)

    is_valid = bool(parsed.get("is_valid", False))
    result = AIValidationResult(
        status="VALID" if is_valid else "INVALID",
        is_valid=is_valid,
        reasoning=parsed.get("reasoning", ""),
        raw=parsed,
    )
    logger.info(
        "[AI-popup] %s — expected=%s observed=%s match=%s reasoning=%s",
        result.status,
        parsed.get("expected_category"),
        parsed.get("observed_category"),
        parsed.get("categories_match"),
        result.reasoning,
    )
    return _persist(result, verdict_output_path)


def _persist(result: AIValidationResult, path: Path | None) -> AIValidationResult:
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(result.to_json(), encoding="utf-8")
        except Exception as e:
            logger.warning("Could not persist popup verdict to %s: %s", path, e)
    return result
