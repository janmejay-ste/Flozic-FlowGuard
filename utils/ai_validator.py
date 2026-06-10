"""
AI-based validation of the connect-creation canvas.

Sends a canvas screenshot to OpenAI's vision-capable chat model and asks
whether the workflow on the canvas matches what the user's prompt requested.
Returns a structured verdict (valid + reasoning) that the test can act on.

Environment:
  OPENAI_API_KEY  — required. If absent, `validate_canvas_with_ai()` returns
                    a 'SKIPPED' verdict so the caller can fall back.
  OPENAI_MODEL    — optional. Default 'gpt-5.4' to match the project's
                    CLAUDE.md. Caller can override per call.

JSON contract returned by the model (enforced via response_format=json_object):
  {
    "is_valid": true|false,
    "trigger_app": "Housecall Pro",
    "action_apps": ["Google Calendar", "Slack"],
    "placeholders_visible": false,
    "reasoning": "<one-sentence explanation>"
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

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.4")
API_BASE = "https://api.openai.com/v1/chat/completions"
REQUEST_TIMEOUT_S = 60


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
    """Read PNG → base64 data URL ready for the chat-completions API."""
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _build_prompt(user_prompt: str, expected_trigger: str) -> str:
    return (
        "You are validating a no-code workflow editor screenshot.\n\n"
        f"The user asked the AI builder to create this workflow:\n"
        f"    \"{user_prompt}\"\n\n"
        f"Expected trigger app: {expected_trigger}\n\n"
        "Look at the canvas in the screenshot and tell me:\n"
        "  1. Is a Trigger card visible with a real app name (not the "
        "placeholder text 'Select Trigger App')?\n"
        "  2. Are the Action card(s) visible with real app names (not "
        "'Select Action App')?\n"
        "  3. Does the trigger app shown match the expected one above?\n\n"
        "Reply with a SINGLE JSON object, no other text, with keys:\n"
        "  is_valid (bool), trigger_app (string), action_apps (list of "
        "strings), placeholders_visible (bool), reasoning (string, one "
        "sentence)."
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

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        logger.info(
            "[AI] OPENAI_API_KEY not set — skipping AI validation. "
            "Set the env var to enable vision-based canvas verification."
        )
        return _persist(AIValidationResult(
            status="SKIPPED",
            is_valid=False,
            reasoning="OPENAI_API_KEY not set; AI validation skipped.",
        ))

    # Defer the requests import so this module is importable on machines
    # without the requests package (e.g. the homepage_links tests' env).
    try:
        import requests  # type: ignore
    except ImportError:
        logger.error(
            "[AI] 'requests' package not installed. Install with: "
            "pip install requests"
        )
        return _persist(AIValidationResult(
            status="ERROR",
            is_valid=False,
            reasoning="'requests' not installed.",
            error="ModuleNotFoundError: requests",
        ))

    if not screenshot_path.exists():
        return _persist(AIValidationResult(
            status="ERROR",
            is_valid=False,
            reasoning=f"Screenshot file not found: {screenshot_path}",
            error="FileNotFoundError",
        ))

    payload = {
        "model": model or DEFAULT_MODEL,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _build_prompt(user_prompt, expected_trigger)},
                    {"type": "image_url",
                     "image_url": {"url": _encode_image(screenshot_path)}},
                ],
            }
        ],
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
        logger.error("[AI] OpenAI request failed: %s", e)
        return _persist(AIValidationResult(
            status="ERROR", is_valid=False,
            reasoning=f"OpenAI request failed: {e}", error=str(e),
        ))

    if resp.status_code != 200:
        body = resp.text[:500]
        logger.error("[AI] OpenAI returned %d: %s", resp.status_code, body)
        return _persist(AIValidationResult(
            status="ERROR", is_valid=False,
            reasoning=f"OpenAI HTTP {resp.status_code}",
            error=body,
        ))

    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        parsed: dict[str, Any] = json.loads(content)
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        logger.error("[AI] Could not parse OpenAI response: %s", e)
        return _persist(AIValidationResult(
            status="ERROR", is_valid=False,
            reasoning=f"Could not parse model output: {e}",
            error=str(e),
        ))

    is_valid = bool(parsed.get("is_valid", False))
    reasoning = str(parsed.get("reasoning", "")).strip() or "(no reasoning given)"
    status = "VALID" if is_valid else "INVALID"
    logger.info("[AI] %s — %s", status, reasoning)
    return _persist(AIValidationResult(
        status=status, is_valid=is_valid, reasoning=reasoning, raw=parsed,
    ))
