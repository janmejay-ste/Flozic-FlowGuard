"""
AI-based validation of the connect-creation canvas.

Sends a canvas screenshot to a vision-capable LLM (Claude or OpenAI —
provider selected by env vars, see utils/ai_provider.py) and asks whether
the workflow on the canvas matches what the user's prompt requested.
Returns a structured verdict (valid + reasoning) that the test can act on.

Environment:
  ANTHROPIC_API_KEY  — enables Claude (preferred if set).
  OPENAI_API_KEY     — enables OpenAI (fallback / legacy).
  FLOZIC_AI_PROVIDER — force one provider: "claude" or "openai".
  CLAUDE_MODEL       — override Claude model (default: claude-sonnet-5).
  OPENAI_MODEL       — override OpenAI model (default: gpt-4o).

If no provider is configured, `validate_canvas_with_ai()` returns a
'SKIPPED' verdict so the caller can fall back to the text-based copilot
check.

JSON contract enforced via provider's structured-output mode:
  {
    "is_valid": true|false,
    "trigger_app": "Housecall Pro",
    "action_apps": ["Google Calendar", "Slack"],
    "placeholders_visible": false,
    "reasoning": "<one-sentence explanation>"
  }
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MODULE = "ai_validator"


@dataclass
class AIValidationResult:
    """Outcome of the AI validation step."""
    status: str             # "VALID" | "INVALID" | "SKIPPED" | "ERROR"
    is_valid: bool          # True only if status == "VALID"
    reasoning: str          # Human-readable explanation
    raw: dict = field(default_factory=dict)   # The model's parsed JSON, if any
    error: str | None = None                  # Any error message

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def _encode_image(path: Path) -> str:
    """Deprecated: base64-encoded data URL. Provider abstraction now
    handles image encoding per-provider. Retained for any external caller."""
    import base64
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _build_prompt(user_prompt: str, expected_trigger: str) -> str:
    """Deprecated shim — the template now lives in `utils.ai_prompts` under
    'canvas_validation'. Retained so external callers keep working."""
    from utils import ai_prompts
    return ai_prompts.render(
        "canvas_validation",
        user_prompt=user_prompt,
        expected_trigger=expected_trigger,
    )


def validate_canvas_with_ai(
    screenshot_path: Path,
    user_prompt: str,
    expected_trigger: str,
    model: str | None = None,
    verdict_output_path: Path | None = None,
) -> AIValidationResult:
    """
    Send the canvas screenshot + prompt to the OpenAI chat-completions API
    and return a structured verdict.

    If `OPENAI_API_KEY` is not set, returns a SKIPPED verdict (the caller
    is expected to then fall back to the text-based copilot-message check).

    If `verdict_output_path` is provided, the verdict JSON is written there
    so the dashboard / artifact browser can show it alongside the screenshot.
    """
    def _persist(result: AIValidationResult) -> AIValidationResult:
        if verdict_output_path is not None:
            try:
                verdict_output_path.parent.mkdir(parents=True, exist_ok=True)
                verdict_output_path.write_text(result.to_json(), encoding="utf-8")
            except Exception as e:
                logger.warning("Could not persist AI verdict to %s: %s",
                               verdict_output_path, e)
        return result

    if not screenshot_path.exists():
        return _persist(AIValidationResult(
            status="ERROR",
            is_valid=False,
            reasoning=f"Screenshot file not found: {screenshot_path}",
            error="FileNotFoundError",
        ))

    # Provider-agnostic send. Either Claude or OpenAI depending on env vars.
    # See utils/ai_provider.py for the selection rules. Missing keys return
    # AIResponse(provider='noop', error=…) — mapped to SKIPPED below.
    from utils import ai_prompts
    from utils.ai_parser import send_json
    from utils.ai_provider import PROVIDER_NAME

    if PROVIDER_NAME == "noop":
        logger.info(
            "[AI] No AI provider configured (set ANTHROPIC_API_KEY or "
            "OPENAI_API_KEY) — skipping AI validation."
        )
        return _persist(AIValidationResult(
            status="SKIPPED",
            is_valid=False,
            reasoning="No AI provider configured; AI validation skipped.",
        ))

    parsed, resp = send_json(
        ai_prompts.render(
            "canvas_validation",
            user_prompt=user_prompt,
            expected_trigger=expected_trigger,
        ),
        module=MODULE,
        model=model,
        image_paths=[screenshot_path],
    )
    if resp is not None and resp.error:
        logger.error("[AI] %s validator request failed: %s",
                     resp.provider, resp.error)
        return _persist(AIValidationResult(
            status="ERROR", is_valid=False,
            reasoning=f"{resp.provider} error: {resp.error}",
            error=resp.error,
        ))
    if not isinstance(parsed, dict):
        logger.error("[AI] %s returned non-JSON response. Raw text: %r",
                     resp.provider if resp else "unknown",
                     (resp.text if resp else "")[:200])
        return _persist(AIValidationResult(
            status="ERROR", is_valid=False,
            reasoning=f"{resp.provider if resp else 'provider'} returned "
                      "non-JSON output",
            error="ResponseParseError",
        ))

    is_valid = bool(parsed.get("is_valid", False))
    reasoning = str(parsed.get("reasoning", "")).strip() or "(no reasoning given)"
    status = "VALID" if is_valid else "INVALID"
    logger.info("[AI] %s — %s", status, reasoning)
    return _persist(AIValidationResult(
        status=status, is_valid=is_valid, reasoning=reasoning, raw=parsed,
    ))
