"""
AI-assisted validation of the PlanChangeService popup that appears after
clicking TRY NOW on the marketing pricing page.

The page-object's keyword classifier (pages.marketing.pricing_page._classify_popup)
makes a fast first guess. This validator is the second opinion: given the
captured popup + the account's current plan + the user's target, it judges
whether the popup category matches the business-logic matrix.

Returns the same AIValidationResult shape as utils.ai_validator so the
dashboard can render popup verdicts identically to canvas verdicts.

Provider-agnostic — Claude or OpenAI, selected by env vars. See
utils/ai_provider.py for the selection rules.

The model REPORTS observations; this module DECIDES the verdict.

That split is deliberate and was learned the hard way. The prompt used to ask
the model for an `is_valid` boolean defined as "true iff categories match AND
plan names correctly mentioned" — a logical conjunction over four fields the
model was already reporting separately. On the 2026-08-11 run all six pricing
variants produced byte-identical observations:

    expected=BLOCK observed=BLOCK match=True current=True target=False

...and the model returned is_valid=true for three of them and false for the
other three. Same evidence, opposite verdicts, at temperature=0. Three tests
failed as "product bugs" on a coin flip.

A deterministic boolean must not come from a sampler. The model now returns
only what it can observe in the popup text; `_decide()` below applies the
policy in Python, so identical evidence always yields an identical verdict
and the rule itself is reviewable in a diff.

JSON contract (from the model — note: no is_valid):
  {
    "expected_category":     "BLOCK" | "CONFIRM_TRIAL" | "CONFIRM_PAID" | ...,
    "observed_category":     "BLOCK" | ...,
    "popup_mentions_target":  true|false,
    "popup_mentions_current": true|false,
    "reasoning":             "<one sentence>"
  }
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path

logger = logging.getLogger(__name__)

MODULE = "ai_popup_validator"

# ── Verdict policy ─────────────────────────────────────────────────────
#
# Which plan names a popup must name, by category. A BLOCK says "you cannot
# leave your current plan" — naming the CURRENT plan is the substance of the
# message; naming the target adds nothing and the product does not do it.
# Requiring it was what made the old rule fail on correct popups.
#
# A CONFIRM/CHECKOUT is the opposite: the user is about to move ONTO the
# target plan, so the target must be named or the confirmation is ambiguous.

_REQUIRES_CURRENT_MENTION = {
    "BLOCK", "BLOCK_PERIOD", "CONFIRM_TRIAL", "CONFIRM_PAID",
    "CONFIRM_SWITCH_YEARLY", "SAME",
}
_REQUIRES_TARGET_MENTION = {
    "CONFIRM_TRIAL", "CONFIRM_PAID", "CONFIRM_SWITCH_YEARLY", "CHECKOUT",
}


def _decide(parsed: dict) -> tuple[bool, list[str]]:
    """
    Apply the verdict policy to the model's observations.

    Returns `(is_valid, failure_reasons)`. Pure function of `parsed` — no
    sampling, no I/O — so the same popup always produces the same verdict.
    """
    expected = str(parsed.get("expected_category", "")).strip().upper()
    observed = str(parsed.get("observed_category", "")).strip().upper()
    mentions_current = bool(parsed.get("popup_mentions_current", False))
    mentions_target = bool(parsed.get("popup_mentions_target", False))

    reasons: list[str] = []

    if not expected or not observed:
        reasons.append(
            f"could not determine categories (expected={expected or '?'}, "
            f"observed={observed or '?'})"
        )
    elif expected != observed:
        reasons.append(f"category mismatch: expected {expected}, got {observed}")

    if observed in _REQUIRES_CURRENT_MENTION and not mentions_current:
        reasons.append(f"{observed} popup does not name the current plan")
    if observed in _REQUIRES_TARGET_MENTION and not mentions_target:
        reasons.append(f"{observed} popup does not name the target plan")

    return (not reasons), reasons


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
    """Deprecated shim — the template and the PlanChangeService matrix now
    live in `utils.ai_prompts` under 'pricing_popup_validation'."""
    from utils import ai_prompts
    return ai_prompts.render(
        "pricing_popup_validation",
        current_plan=current_plan,
        target_plan=target_plan,
        target_period=target_period,
        page_classifier=page_classifier,
        popup_title=repr(popup_title),
        popup_message=repr(popup_message),
        popup_primary=repr(popup_primary),
        popup_secondary=repr(popup_secondary),
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
    Send the captured popup for matrix-aware validation.

    Returns SKIPPED when no AI provider is configured — the caller falls
    back to the page-object keyword classifier alone in that case.
    """
    from utils import ai_prompts
    from utils.ai_parser import send_json
    from utils.ai_provider import PROVIDER_NAME

    if PROVIDER_NAME == "noop":
        return _persist(AIValidationResult(
            status="SKIPPED", is_valid=False,
            reasoning="No AI provider configured — popup validated by "
                      "keyword classifier only.",
        ), verdict_output_path)

    prompt = ai_prompts.render(
        "pricing_popup_validation",
        current_plan=current_plan,
        target_plan=target_plan,
        target_period=target_period,
        page_classifier=snapshot.category_guess,
        popup_title=repr(snapshot.title),
        popup_message=repr(snapshot.message),
        popup_primary=repr(snapshot.primary_action),
        popup_secondary=repr(snapshot.secondary_action),
    )

    parsed, resp = send_json(prompt, module=MODULE, model=model)

    if resp is not None and resp.error:
        logger.error("[AI-popup] %s request failed: %s", resp.provider, resp.error)
        return _persist(AIValidationResult(
            status="ERROR", is_valid=False,
            reasoning=f"{resp.provider} request failed: {resp.error}",
            error=resp.error,
        ), verdict_output_path)

    if not isinstance(parsed, dict):
        logger.error("[AI-popup] Could not parse %s response as JSON.",
                     resp.provider if resp else "provider")
        return _persist(AIValidationResult(
            status="ERROR", is_valid=False,
            reasoning="Could not parse model output as JSON.",
            error="ResponseParseError",
        ), verdict_output_path)

    # The verdict is computed here, not sampled — see the module docstring.
    is_valid, failures = _decide(parsed)

    model_reasoning = str(parsed.get("reasoning", "")).strip()
    reasoning = model_reasoning if is_valid else "; ".join(failures)

    # Record both the model's observations and the policy decision, so a
    # verdict on the dashboard can be traced to the rule that produced it
    # rather than to an opaque boolean.
    raw = dict(parsed)
    raw["_decision"] = {
        "is_valid": is_valid,
        "failures": failures,
        "policy": {
            "requires_current_mention": sorted(_REQUIRES_CURRENT_MENTION),
            "requires_target_mention": sorted(_REQUIRES_TARGET_MENTION),
        },
        "model_reasoning": model_reasoning,
        "decided_by": "utils.ai_popup_validator._decide",
    }

    result = AIValidationResult(
        status="VALID" if is_valid else "INVALID",
        is_valid=is_valid,
        reasoning=reasoning,
        raw=raw,
    )
    logger.info(
        "[AI-popup] %s — expected=%s observed=%s current=%s target=%s | %s",
        result.status,
        parsed.get("expected_category"),
        parsed.get("observed_category"),
        parsed.get("popup_mentions_current"),
        parsed.get("popup_mentions_target"),
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
