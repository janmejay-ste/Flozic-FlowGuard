"""
Thin Anthropic Messages API wrapper.

Kept intentionally minimal so `utils/ai_provider.py` can swap OpenAI ↔ Claude
without callers noticing. Any behavior specific to Claude (message shape,
vision-encoding, tool-use, cache-control, extended thinking) lives here.

Not imported unless the caller picks the Claude provider — so a machine
without the `anthropic` package installed can still run OpenAI-backed tests.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default to Sonnet 5 (fast + capable + cheap for vision + JSON). Callers
# can override via CLAUDE_MODEL env or per-call `model=` argument.
DEFAULT_CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
DEFAULT_MAX_TOKENS = 2048
REQUEST_TIMEOUT_S  = 60


@dataclass
class ClaudeResponse:
    """Uniform result envelope. Callers should check `error` first."""
    text: str = ""
    parsed_json: dict[str, Any] | None = None
    model: str = ""
    stop_reason: str = ""
    input_tokens:  int = 0
    output_tokens: int = 0
    error: str | None = None


def send(
    prompt: str,
    *,
    model:          str | None = None,
    image_paths:    list[Path] | None = None,
    response_json:  bool = True,
    temperature:    float = 0.0,
    max_tokens:     int   = DEFAULT_MAX_TOKENS,
    system_prompt:  str   | None = None,
) -> ClaudeResponse:
    """
    Send a single message to Claude and return a structured response.

    Args:
      prompt         — user message text
      model          — override CLAUDE_MODEL default
      image_paths    — optional list of PNG/JPG paths, base64-encoded and
                       attached inline. Order preserved.
      response_json  — if True, parse `text` as JSON into `parsed_json`.
                       Non-fatal on parse failure — `parsed_json` stays None.
      temperature    — Anthropic default is 1.0; we default to 0 for
                       deterministic verdicts (matches OpenAI provider).
      max_tokens     — hard cap; JSON verdicts rarely exceed 512.
      system_prompt  — optional system message.

    Returns ClaudeResponse. `.error` is set for transport / API failures;
    JSON parse failures leave `.text` populated and `.parsed_json = None`.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return ClaudeResponse(
            error="ANTHROPIC_API_KEY not set",
        )

    try:
        import anthropic
    except ImportError:
        return ClaudeResponse(
            error="anthropic package not installed. `pip install anthropic`",
        )

    # Build content blocks — text always first, then images.
    content: list[dict[str, Any]] = []
    for p in (image_paths or []):
        try:
            block = _image_block(Path(p))
            if block is not None:
                content.append(block)
        except Exception as e:
            logger.warning("[claude] image encode failed for %s: %s", p, e)
    content.append({"type": "text", "text": prompt})

    kwargs: dict[str, Any] = {
        "model": model or DEFAULT_CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": content}],
    }
    if system_prompt:
        kwargs["system"] = system_prompt

    client = anthropic.Anthropic(api_key=api_key, timeout=REQUEST_TIMEOUT_S)
    try:
        msg = client.messages.create(**kwargs)
    except Exception as e:
        logger.error("[claude] messages.create failed: %s", e)
        return ClaudeResponse(error=str(e), model=kwargs["model"])

    # Extract text from response — Claude returns a list of content blocks.
    text_parts = []
    for block in getattr(msg, "content", []) or []:
        if getattr(block, "type", None) == "text":
            text_parts.append(getattr(block, "text", ""))
    text = "".join(text_parts).strip()

    usage = getattr(msg, "usage", None)
    resp = ClaudeResponse(
        text          = text,
        model         = getattr(msg, "model", kwargs["model"]) or kwargs["model"],
        stop_reason   = getattr(msg, "stop_reason", "") or "",
        input_tokens  = getattr(usage, "input_tokens",  0) if usage else 0,
        output_tokens = getattr(usage, "output_tokens", 0) if usage else 0,
    )

    if response_json:
        # Claude sometimes wraps JSON in ```json fences even when the prompt
        # asks for raw JSON, and occasionally adds a sentence around it.
        # utils/ai_parser handles fences, prose, and the usual malformations
        # in one place so both providers behave identically here.
        from utils.ai_parser import parse_json
        parsed = parse_json(text)
        if parsed is None and text:
            logger.warning(
                "[claude] response_json=True but text was not valid JSON "
                "(first 120 chars: %r)", text[:120],
            )
        resp.parsed_json = parsed if isinstance(parsed, dict) else None
    return resp


# ── Helpers ────────────────────────────────────────────────────────────


def _image_block(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        logger.warning("[claude] image not found: %s", path)
        return None
    suffix = path.suffix.lower().lstrip(".")
    media_type = {
        "png":  "image/png",
        "jpg":  "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
        "gif":  "image/gif",
    }.get(suffix, "image/png")
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": b64},
    }


def _strip_json_fences(text: str) -> str:
    """Deprecated shim — fence handling moved to `utils.ai_parser` so both
    providers share one implementation. Kept for any external caller."""
    from utils.ai_parser import strip_json_fences
    return strip_json_fences(text)
